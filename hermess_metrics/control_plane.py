"""HTTP client for the Axiom endpoints setup needs."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_DOMAIN = "api.axiom.co"
DEFAULT_TIMEOUT = 15.0
DEFAULT_BACKOFF = 0.5
MAX_ATTEMPTS = 3
RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})

# Each needs a different permission, so one succeeding proves the token itself is fine.
PROBE_PATHS = ("/v2/tokens", "/v2/datasets", "/v2/dashboards")

PERMISSION_DATASETS = "datasets:create"
PERMISSION_TOKENS = "apiTokens:create"

DATASET_KINDS = {
    "traces": "otel:traces:v1",
    "logs": "otel:logs:v1",
    "metrics": "otel:metrics:v1",
}

logger = logging.getLogger(__name__)


class AxiomError(RuntimeError):
    """A control-plane call that did not succeed."""

    def __init__(
        self,
        status: int,
        message: str,
        resets_at: int | None = None,
        operation: str = "",
        permission: str = "",
        trace_id: str = "",
    ) -> None:
        where = f"{operation} failed: " if operation else ""
        super().__init__(f"{where}axiom returned {status}: {message}")
        self.status = status
        self.message = message
        self.resets_at = resets_at
        self.operation = operation
        self.permission = permission
        self.trace_id = trace_id

    @property
    def seconds_until_reset(self) -> float:
        return max(0.0, self.resets_at - time.time()) if self.resets_at else 0.0


@dataclass(frozen=True)
class ProvisionedOrg:
    """A temporary org, its claim link, and the token it was issued with."""

    id: str
    name: str
    region: str
    expires_at: str
    claim_url: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class ControlPlane:
    """Calls the Axiom v2 API on behalf of the operator."""

    domain: str = DEFAULT_DOMAIN
    token: str = field(default="", repr=False)
    org: str = ""
    scheme: str = "https"
    timeout: float = DEFAULT_TIMEOUT
    backoff: float = DEFAULT_BACKOFF

    def provision_org(self, name: str | None = None, region: str | None = None) -> ProvisionedOrg:
        """Create a temporary org. This endpoint takes no credentials."""
        payload: dict[str, Any] = {}
        if name:
            payload["name"] = name
        if region:
            payload["edgeDeployment"] = region
        body = self._post(
            "/v2/orgs/provision",
            payload,
            authenticated=False,
            operation="provisioning an org",
        )
        try:
            return ProvisionedOrg(
                id=body["id"],
                name=body["name"],
                region=body["defaultEdgeDeployment"],
                expires_at=body["expiresAt"],
                claim_url=body["claimUrl"],
                token=body["token"],
            )
        except (KeyError, TypeError) as exc:
            raise AxiomError(200, f"provision response missing {exc}") from exc

    def create_dataset(self, name: str, kind: str, description: str = "") -> dict[str, Any]:
        payload = {"name": name, "kind": kind, "description": description}
        result = self._post(
            "/v2/datasets",
            payload,
            operation=f"creating dataset {name!r}",
            permission=PERMISSION_DATASETS,
        )
        return result if isinstance(result, dict) else {}

    def create_ingest_token(
        self, name: str, datasets: list[str], description: str = "", with_query: bool = True
    ) -> str:
        """Mint a token scoped to these datasets, and nothing else."""
        capability: dict[str, list[str]] = {"ingest": ["create"]}
        if with_query:
            capability["query"] = ["read"]
        payload = {
            "name": name,
            "description": description,
            "datasetCapabilities": {dataset: dict(capability) for dataset in datasets},
        }
        body = self._post(
            "/v2/tokens",
            payload,
            operation="creating an ingest token",
            permission=PERMISSION_TOKENS,
        )
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise AxiomError(200, "token response carried no token")
        return token

    def token_is_accepted(self) -> bool | None:
        """True if any read succeeded, False if all were refused, None if unreachable."""
        refused = False
        for path in PROBE_PATHS:
            try:
                self._request("GET", path, None, operation="probing access")
            except AxiomError as exc:
                if exc.status in (401, 403):
                    refused = True
                    continue
                return None
            else:
                return True
        return False if refused else None

    def _post(
        self,
        path: str,
        payload: Any,
        authenticated: bool = True,
        operation: str = "",
        permission: str = "",
    ) -> Any:
        url = f"{self.scheme}://{self.domain}{path}"
        data = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self.token}"
            if self.org:
                headers["X-Axiom-Org-Id"] = self.org
        last: AxiomError | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                return self._once(url, data, headers, operation, permission)
            except AxiomError as exc:
                last = exc
                if exc.status not in RETRY_STATUS or _resets_beyond_backoff(exc, self.backoff):
                    raise
            if attempt + 1 < MAX_ATTEMPTS and self.backoff:
                time.sleep(self.backoff * (2**attempt))
        raise last if last is not None else AxiomError(0, "no attempt was made")

    def _request(
        self,
        method: str,
        path: str,
        data: bytes | None,
        operation: str = "",
        permission: str = "",
    ) -> Any:
        url = f"{self.scheme}://{self.domain}{path}"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"}
        if self.org:
            headers["X-Axiom-Org-Id"] = self.org
        return self._once(url, data, headers, operation, permission, method)

    def _once(
        self,
        url: str,
        data: bytes | None,
        headers: dict[str, str],
        operation: str = "",
        permission: str = "",
        method: str = "POST",
    ) -> Any:
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode() or "{}"
        except urllib.error.HTTPError as exc:
            raise AxiomError(
                exc.code,
                _message(exc.read()),
                _resets_at(exc.headers),
                operation,
                permission,
                _trace_id(exc.headers),
            ) from exc
        except OSError as exc:
            raise AxiomError(0, str(exc), None, operation, permission) from exc
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise AxiomError(200, "response was not json", None, operation, permission) from exc


def _trace_id(headers: Any) -> str:
    try:
        return str(headers.get("x-axiom-trace-id") or "")
    except (AttributeError, TypeError):
        return ""


def _resets_at(headers: Any) -> int | None:
    try:
        return int(headers.get("x-ratelimit-reset") or 0) or None
    except (AttributeError, TypeError, ValueError):
        return None


def _resets_beyond_backoff(error: AxiomError, backoff: float) -> bool:
    """Retrying inside this call cannot outlast a window that resets much later."""
    budget = backoff * (2**MAX_ATTEMPTS)
    return bool(error.seconds_until_reset > max(budget, 1.0))


def _message(raw: bytes) -> str:
    try:
        body = json.loads(raw.decode() or "{}")
    except ValueError:
        return raw.decode(errors="replace")[:200]
    return str(body.get("message") or body)[:200] if isinstance(body, dict) else str(body)[:200]
