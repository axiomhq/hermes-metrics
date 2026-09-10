"""HTTP client for the Axiom endpoints setup needs."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from hermess_metrics.control_plane import (
    DATASET_KINDS,
    AxiomError,
    ControlPlane,
    ProvisionedOrg,
)

PROVISIONED = {
    "id": "probe-org-x1",
    "name": "probe-org",
    "defaultEdgeDeployment": "cloud.us-east-1.aws",
    "expiresAt": "2026-09-11T10:50:37.466Z",
    "claimUrl": "https://app.axiom.co/orgs/probe-org-x1/claim?token=abc",
    "token": "xaat-full-permission",
}


class _Handler(BaseHTTPRequestHandler):
    script: dict[str, tuple[int, Any]] = {}
    seen: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        type(self).seen.append(
            {
                "path": self.path,
                "body": json.loads(body) if body else None,
                "headers": {k.lower(): v for k, v in self.headers.items()},
            }
        )
        status, payload = type(self).script.get(self.path, (404, {"message": "no route"}))
        if isinstance(status, list):
            status, payload = status.pop(0) if status else (500, {})
        encoded = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture
def server():
    _Handler.script = {}
    _Handler.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host = f"127.0.0.1:{httpd.server_port}"
    yield host, _Handler
    httpd.shutdown()


def _plane(host: str, token: str = "") -> ControlPlane:
    return ControlPlane(domain=host, token=token, scheme="http", backoff=0.0)


def test_dataset_kinds_cover_every_signal() -> None:
    assert DATASET_KINDS == {
        "traces": "otel:traces:v1",
        "logs": "otel:logs:v1",
        "metrics": "otel:metrics:v1",
    }


def test_provisioning_an_org_needs_no_token(server) -> None:
    host, handler = server
    handler.script["/v2/orgs/provision"] = (200, PROVISIONED)
    org = _plane(host).provision_org(name="probe-org")
    assert isinstance(org, ProvisionedOrg)
    assert org.id == "probe-org-x1"
    assert org.claim_url.endswith("token=abc")
    assert org.region == "cloud.us-east-1.aws"
    assert "authorization" not in handler.seen[0]["headers"]


def test_the_provision_name_and_region_are_sent(server) -> None:
    host, handler = server
    handler.script["/v2/orgs/provision"] = (200, PROVISIONED)
    _plane(host).provision_org(name="probe-org", region="cloud.eu-central-1.aws")
    assert handler.seen[0]["body"] == {
        "name": "probe-org",
        "edgeDeployment": "cloud.eu-central-1.aws",
    }


def test_creating_a_dataset_sends_its_kind_and_token(server) -> None:
    host, handler = server
    handler.script["/v2/datasets"] = (200, {"name": "hermes-traces", "kind": "otel:traces:v1"})
    plane = _plane(host, token="xaat-admin")
    plane.create_dataset("hermes-traces", DATASET_KINDS["traces"], "Hermes telemetry")
    request = handler.seen[0]
    assert request["body"]["kind"] == "otel:traces:v1"
    assert request["headers"]["authorization"] == "Bearer xaat-admin"


def test_an_ingest_token_is_scoped_to_the_named_datasets(server) -> None:
    host, handler = server
    handler.script["/v2/tokens"] = (200, {"token": "xaat-scoped", "id": "tok1"})
    token = _plane(host, token="xaat-admin").create_ingest_token("ingest", ["a", "b"])
    assert token == "xaat-scoped"
    capabilities = handler.seen[0]["body"]["datasetCapabilities"]
    assert capabilities == {"a": {"ingest": ["create"]}, "b": {"ingest": ["create"]}}


def test_a_client_error_is_raised_with_its_message(server) -> None:
    host, handler = server
    handler.script["/v2/datasets"] = (403, {"code": 403, "message": "not allowed"})
    with pytest.raises(AxiomError) as caught:
        _plane(host, token="t").create_dataset("x", DATASET_KINDS["logs"])
    assert caught.value.status == 403
    assert "not allowed" in str(caught.value)


def test_a_client_error_is_not_retried(server) -> None:
    host, handler = server
    handler.script["/v2/datasets"] = (400, {"message": "bad name"})
    with pytest.raises(AxiomError):
        _plane(host, token="t").create_dataset("x", DATASET_KINDS["logs"])
    assert len(handler.seen) == 1


def test_a_server_error_is_retried_then_raised(server) -> None:
    host, handler = server
    handler.script["/v2/datasets"] = ([(500, {}), (500, {}), (500, {})], None)
    with pytest.raises(AxiomError):
        _plane(host, token="t").create_dataset("x", DATASET_KINDS["logs"])
    assert len(handler.seen) == 3


def test_a_transient_failure_is_retried_into_success(server) -> None:
    host, handler = server
    handler.script["/v2/datasets"] = ([(503, {}), (200, {"name": "x"})], None)
    result = _plane(host, token="t").create_dataset("x", DATASET_KINDS["logs"])
    assert result["name"] == "x"
    assert len(handler.seen) == 2


def test_rate_limiting_is_retried(server) -> None:
    host, handler = server
    handler.script["/v2/orgs/provision"] = ([(429, {}), (200, PROVISIONED)], None)
    assert _plane(host).provision_org().id == "probe-org-x1"


def test_an_unreachable_host_raises_an_axiom_error() -> None:
    plane = ControlPlane(domain="127.0.0.1:1", token="t", scheme="http", backoff=0.0)
    with pytest.raises(AxiomError):
        plane.create_dataset("x", DATASET_KINDS["logs"])


def test_a_malformed_response_raises_an_axiom_error(server) -> None:
    host, handler = server
    handler.script["/v2/orgs/provision"] = (200, {"id": "only-an-id"})
    with pytest.raises(AxiomError):
        _plane(host).provision_org()


def test_the_token_stays_out_of_reprs() -> None:
    org = ProvisionedOrg(
        id="i", name="n", region="r", expires_at="e", claim_url="c", token="xaat-secret"
    )
    assert "xaat-secret" not in repr(org)
    assert "xaat-secret" not in repr(ControlPlane(domain="d", token="xaat-secret"))


def test_a_token_response_without_a_token_is_an_error(server) -> None:
    host, handler = server
    handler.script["/v2/tokens"] = (200, {"id": "tok1"})
    with pytest.raises(AxiomError):
        _plane(host, token="t").create_ingest_token("ingest", ["a"])


def test_a_non_json_response_is_an_error(server) -> None:
    host, handler = server
    handler.script["/v2/datasets"] = (200, b"<html>not json</html>")
    with pytest.raises(AxiomError) as caught:
        _plane(host, token="t").create_dataset("x", DATASET_KINDS["logs"])
    assert "not json" in str(caught.value)


def test_a_non_json_error_body_is_reported_verbatim(server) -> None:
    host, handler = server
    handler.script["/v2/datasets"] = (500, b"plain text explosion")
    with pytest.raises(AxiomError) as caught:
        ControlPlane(domain=host, token="t", scheme="http", backoff=0.0).create_dataset(
            "x", DATASET_KINDS["logs"]
        )
    assert caught.value.status == 500
    assert "plain text explosion" in caught.value.message


def test_backoff_is_applied_between_attempts(server, monkeypatch: pytest.MonkeyPatch) -> None:
    host, handler = server
    slept: list[float] = []
    monkeypatch.setattr("hermess_metrics.control_plane.time.sleep", slept.append)
    handler.script["/v2/datasets"] = ([(503, {}), (200, {"name": "x"})], None)
    ControlPlane(domain=host, token="t", scheme="http", backoff=0.25).create_dataset(
        "x", DATASET_KINDS["logs"]
    )
    assert slept == [0.25]
