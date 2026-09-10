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

DATASET_KINDS = {
    "traces": "otel:traces:v1",
    "logs": "otel:logs:v1",
    "metrics": "otel:metrics:v1",
}

logger = logging.getLogger(__name__)


class AxiomError(RuntimeError):
    """A control-plane call that did not succeed."""

    def __init__(self, status: int, message: str, resets_at: int | None = None) -> None:
        super().__init__(f"axiom returned {status}: {message}")
        self.status = status
        self.message = message
        self.resets_at = resets_at

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
        body = self._post("/v2/orgs/provision", payload, authenticated=False)
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
        result = self._post("/v2/datasets", payload)
        return result if isinstance(result, dict) else {}

    def create_ingest_token(self, name: str, datasets: list[str], description: str = "") -> str:
        """Mint a token that writes and reads these datasets, and nothing else."""
        payload = {
            "name": name,
            "description": description,
            "datasetCapabilities": {
                dataset: {"ingest": ["create"], "query": ["read"]} for dataset in datasets
            },
        }
        body = self._post("/v2/tokens", payload)
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise AxiomError(200, "token response carried no token")
        return token

    def _post(self, path: str, payload: Any, authenticated: bool = True) -> Any:
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
                return self._once(url, data, headers)
            except AxiomError as exc:
                last = exc
                if exc.status not in RETRY_STATUS or _resets_beyond_backoff(exc, self.backoff):
                    raise
            if attempt + 1 < MAX_ATTEMPTS and self.backoff:
                time.sleep(self.backoff * (2**attempt))
        raise last if last is not None else AxiomError(0, "no attempt was made")

    def _once(self, url: str, data: bytes, headers: dict[str, str]) -> Any:
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode() or "{}"
        except urllib.error.HTTPError as exc:
            raise AxiomError(exc.code, _message(exc.read()), _resets_at(exc.headers)) from exc
        except OSError as exc:
            raise AxiomError(0, str(exc)) from exc
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise AxiomError(200, "response was not json") from exc


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
