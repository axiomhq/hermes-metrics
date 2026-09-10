# SPDX-License-Identifier: Apache-2.0 OR MIT
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

    def do_GET(self) -> None:  # noqa: N802
        self.do_POST()

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
    assert capabilities == {
        "a": {"ingest": ["create"], "query": ["read"]},
        "b": {"ingest": ["create"], "query": ["read"]},
    }


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


class _LimitHandler(BaseHTTPRequestHandler):
    resets_at: int = 0
    seen: list[str] = []

    def do_POST(self) -> None:  # noqa: N802
        type(self).seen.append(self.path)
        self.send_response(429)
        if type(self).resets_at:
            self.send_header("x-ratelimit-limit", "3")
            self.send_header("x-ratelimit-remaining", "0")
            self.send_header("x-ratelimit-reset", str(type(self).resets_at))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture
def limited():
    _LimitHandler.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _LimitHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{httpd.server_port}", _LimitHandler
    httpd.shutdown()


def test_a_far_off_rate_limit_is_not_retried(limited) -> None:
    import time as _time

    host, handler = limited
    handler.resets_at = int(_time.time()) + 3600
    with pytest.raises(AxiomError) as caught:
        _plane(host).provision_org()
    assert len(handler.seen) == 1
    assert caught.value.status == 429
    assert 3500 < caught.value.seconds_until_reset <= 3600


def test_a_rate_limit_without_headers_is_still_retried(limited) -> None:
    host, handler = limited
    handler.resets_at = 0
    with pytest.raises(AxiomError) as caught:
        _plane(host).provision_org()
    assert len(handler.seen) == 3
    assert caught.value.seconds_until_reset == 0.0


def test_an_imminent_rate_limit_is_retried(limited) -> None:
    import time as _time

    host, handler = limited
    handler.resets_at = int(_time.time())
    with pytest.raises(AxiomError):
        _plane(host).provision_org()
    assert len(handler.seen) == 3


def test_an_unreadable_rate_limit_header_is_ignored() -> None:
    from hermess_metrics.control_plane import _resets_at

    class Headers:
        def get(self, name: str) -> str:
            return "not-a-number"

    assert _resets_at(Headers()) is None
    assert _resets_at(None) is None


def test_an_org_id_is_sent_when_given(server) -> None:
    host, handler = server
    handler.script["/v2/datasets"] = (200, {"name": "x"})
    ControlPlane(domain=host, token="t", org="my-org-7", scheme="http", backoff=0.0).create_dataset(
        "x", DATASET_KINDS["logs"]
    )
    assert handler.seen[0]["headers"]["x-axiom-org-id"] == "my-org-7"


def test_no_org_header_is_sent_when_absent(server) -> None:
    host, handler = server
    handler.script["/v2/datasets"] = (200, {"name": "x"})
    _plane(host, token="t").create_dataset("x", DATASET_KINDS["logs"])
    assert "x-axiom-org-id" not in handler.seen[0]["headers"]


def test_a_token_accepted_by_any_read_is_reported_as_accepted(server) -> None:
    class _ReadHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"[]")

        def log_message(self, *a: Any) -> None:
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ReadHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        plane = ControlPlane(
            domain=f"127.0.0.1:{httpd.server_port}", token="t", scheme="http", backoff=0.0
        )
        assert plane.token_is_accepted() is True
    finally:
        httpd.shutdown()


def test_an_unreadable_trace_header_is_ignored() -> None:
    from hermess_metrics.control_plane import _trace_id

    assert _trace_id(None) == ""


def test_a_token_refused_by_every_read_is_reported_as_refused(server) -> None:
    host, handler = server
    for path in ("/v2/tokens", "/v2/datasets", "/v2/dashboards"):
        handler.script[path] = (403, {"message": "forbidden"})
    plane = ControlPlane(domain=host, token="t", scheme="http", backoff=0.0)
    assert plane.token_is_accepted() is False


def test_an_unreachable_host_gives_no_verdict() -> None:
    plane = ControlPlane(domain="127.0.0.1:1", token="t", scheme="http", backoff=0.0)
    assert plane.token_is_accepted() is None


def test_creating_a_monitor_sends_it_and_names_the_permission(server) -> None:
    host, handler = server
    handler.script["/v2/monitors"] = (200, {"id": "mon1"})
    plane = _plane(host, token="t")
    result = plane.create_monitor({"name": "x", "type": "Threshold"})
    assert result["id"] == "mon1"
    assert handler.seen[0]["body"]["name"] == "x"


def test_a_refused_monitor_names_the_permission_it_needed(server) -> None:
    from hermess_metrics.control_plane import PERMISSION_MONITORS

    host, handler = server
    handler.script["/v2/monitors"] = (403, {"message": "nope"})
    with pytest.raises(AxiomError) as caught:
        _plane(host, token="t").create_monitor({"name": "x", "type": "Threshold"})
    assert caught.value.permission == PERMISSION_MONITORS


def test_a_minted_token_can_also_alert(server) -> None:
    """Otherwise the very next command needs a different token."""
    from hermess_metrics.control_plane import ALERTING_CAPABILITIES

    host, handler = server
    handler.script["/v2/tokens"] = (200, {"token": "xaat-scoped"})
    _plane(host, token="t").create_ingest_token("ingest", ["a"])
    assert handler.seen[0]["body"]["orgCapabilities"] == ALERTING_CAPABILITIES


def test_alerting_can_be_left_off(server) -> None:
    host, handler = server
    handler.script["/v2/tokens"] = (200, {"token": "xaat-scoped"})
    _plane(host, token="t").create_ingest_token("ingest", ["a"], with_alerting=False)
    assert "orgCapabilities" not in handler.seen[0]["body"]
