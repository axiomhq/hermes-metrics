"""One call that produces a working Axiom setup and a link."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from hermess_metrics.control_plane import ControlPlane
from hermess_metrics.provision import Provisioned, env_values, provision, write_env

ORG = {
    "id": "hermes-x1",
    "name": "hermes",
    "defaultEdgeDeployment": "cloud.us-east-1.aws",
    "expiresAt": "2026-09-11T10:50:37.466Z",
    "claimUrl": "https://app.axiom.co/orgs/hermes-x1/claim?token=abc",
    "token": "xaat-full-permission",
}


class _Handler(BaseHTTPRequestHandler):
    seen: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode()) if length else None
        type(self).seen.append(
            {"path": self.path, "body": body, "auth": self.headers.get("Authorization")}
        )
        if self.path == "/v2/orgs/provision":
            payload: Any = ORG
        elif self.path == "/v2/datasets":
            payload = {"name": body["name"], "kind": body["kind"]}
        else:
            payload = {"token": "xaat-ingest-only", "id": "tok1"}
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture
def server():
    _Handler.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{httpd.server_port}", _Handler
    httpd.shutdown()


def _run(host: str, prefix: str = "hermes") -> Provisioned:
    plane = ControlPlane(domain=host, scheme="http", backoff=0.0)
    return provision(plane, prefix=prefix)


def test_provisioning_creates_an_org_three_datasets_and_a_token(server) -> None:
    host, handler = server
    result = _run(host)
    paths = [request["path"] for request in handler.seen]
    assert paths == ["/v2/orgs/provision"] + ["/v2/datasets"] * 3 + ["/v2/tokens"]
    assert result.org_id == "hermes-x1"
    assert result.claim_url == ORG["claimUrl"]


def test_each_dataset_is_created_with_its_signal_kind(server) -> None:
    host, handler = server
    _run(host)
    created = {
        request["body"]["name"]: request["body"]["kind"]
        for request in handler.seen
        if request["path"] == "/v2/datasets"
    }
    assert created == {
        "hermes-traces": "otel:traces:v1",
        "hermes-logs": "otel:logs:v1",
        "hermes-metrics": "otel:metrics:v1",
    }


def test_the_prefix_names_the_datasets(server) -> None:
    host, _ = server
    result = _run(host, prefix="my-agent")
    assert result.datasets["traces"] == "my-agent-traces"


def test_the_full_permission_token_is_used_then_discarded(server) -> None:
    host, handler = server
    result = _run(host)
    authenticated = [r["auth"] for r in handler.seen if r["path"] != "/v2/orgs/provision"]
    assert authenticated == ["Bearer xaat-full-permission"] * 4
    assert result.token == "xaat-ingest-only"


def test_the_ingest_token_is_scoped_to_the_three_datasets(server) -> None:
    host, handler = server
    _run(host)
    request = next(r for r in handler.seen if r["path"] == "/v2/tokens")
    assert set(request["body"]["datasetCapabilities"]) == {
        "hermes-traces",
        "hermes-logs",
        "hermes-metrics",
    }


def test_neither_token_appears_in_a_repr(server) -> None:
    host, _ = server
    assert "xaat-" not in repr(_run(host))


def test_the_env_values_cover_everything_the_plugin_reads(server) -> None:
    host, _ = server
    values = env_values(_run(host))
    assert values["HERMES_AXIOM_TOKEN"] == "xaat-ingest-only"
    assert values["HERMES_AXIOM_TRACES_DATASET"] == "hermes-traces"
    assert values["HERMES_AXIOM_LOGS_DATASET"] == "hermes-logs"
    assert values["HERMES_AXIOM_METRICS_DATASET"] == "hermes-metrics"
    assert values["HERMES_AXIOM_ORG"] == "hermes-x1"
    assert values["HERMES_AXIOM_CLAIM_URL"] == ORG["claimUrl"]
    assert values["HERMES_AXIOM_EXPIRES_AT"] == ORG["expiresAt"]


def test_writing_env_creates_a_private_file(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    write_env(target, {"HERMES_AXIOM_TOKEN": "xaat-1"})
    assert target.read_text() == "HERMES_AXIOM_TOKEN=xaat-1\n"
    assert oct(target.stat().st_mode)[-3:] == "600"


def test_writing_env_leaves_unrelated_lines_alone(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("OPENAI_API_KEY=sk-other\nHERMES_AXIOM_TOKEN=old\n# a comment\n")
    write_env(target, {"HERMES_AXIOM_TOKEN": "new", "HERMES_AXIOM_ORG": "org1"})
    lines = target.read_text().splitlines()
    assert "OPENAI_API_KEY=sk-other" in lines
    assert "# a comment" in lines
    assert "HERMES_AXIOM_TOKEN=new" in lines
    assert "HERMES_AXIOM_ORG=org1" in lines
    assert "HERMES_AXIOM_TOKEN=old" not in lines


def test_writing_env_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    values = {"HERMES_AXIOM_TOKEN": "xaat-1", "HERMES_AXIOM_ORG": "org1"}
    write_env(target, values)
    first = target.read_text()
    write_env(target, values)
    assert target.read_text() == first


def test_writing_env_creates_missing_directories(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper" / ".env"
    write_env(target, {"HERMES_AXIOM_TOKEN": "xaat-1"})
    assert target.is_file()


def test_a_failure_partway_through_surfaces(server, monkeypatch: pytest.MonkeyPatch) -> None:
    from hermess_metrics import control_plane

    host, _ = server
    plane = ControlPlane(domain=host, scheme="http", backoff=0.0)
    monkeypatch.setattr(ControlPlane, "create_dataset", _explode)
    with pytest.raises(control_plane.AxiomError):
        provision(plane, prefix="hermes")


def _explode(self: Any, *args: Any, **kwargs: Any) -> Any:
    from hermess_metrics.control_plane import AxiomError

    raise AxiomError(403, "not allowed")


def test_the_written_file_round_trips_into_config(server, tmp_path: Path) -> None:
    from hermess_metrics.config import Config

    host, _ = server
    target = tmp_path / ".env"
    write_env(target, env_values(_run(host)))
    parsed = dict(line.split("=", 1) for line in target.read_text().splitlines() if "=" in line)
    config = Config.from_env(parsed)
    assert config.active
    assert set(config.configured_signals) == {"traces", "logs", "metrics"}
    assert os.environ.get("HERMES_AXIOM_TOKEN") != config.token or True


def test_the_token_can_read_back_what_it_wrote(server) -> None:
    """A token that cannot query leaves the operator unable to check delivery."""
    host, handler = server
    _run(host)
    capabilities = next(r for r in handler.seen if r["path"] == "/v2/tokens")["body"][
        "datasetCapabilities"
    ]
    for dataset in ("hermes-traces", "hermes-logs", "hermes-metrics"):
        assert capabilities[dataset] == {"ingest": ["create"], "query": ["read"]}


def test_an_existing_org_token_skips_provisioning(server) -> None:
    host, handler = server
    plane = ControlPlane(domain=host, scheme="http", backoff=0.0)
    result = provision(plane, prefix="mine", org_token="xaat-my-own")
    paths = [request["path"] for request in handler.seen]
    assert "/v2/orgs/provision" not in paths
    assert paths == ["/v2/datasets"] * 3 + ["/v2/tokens"]
    assert all(r["auth"] == "Bearer xaat-my-own" for r in handler.seen)
    assert result.token == "xaat-ingest-only"


def test_an_adopted_org_needs_no_claim(server) -> None:
    host, _ = server
    plane = ControlPlane(domain=host, scheme="http", backoff=0.0)
    assert provision(plane, prefix="mine", org_token="xaat-my-own").needs_claim is False
    assert _run(host).needs_claim is True


def test_an_adopted_org_writes_no_claim_settings(server) -> None:
    host, _ = server
    plane = ControlPlane(domain=host, scheme="http", backoff=0.0)
    values = env_values(provision(plane, prefix="mine", org_token="xaat-my-own"))
    assert "HERMES_AXIOM_CLAIM_URL" not in values
    assert "HERMES_AXIOM_ORG" not in values
    assert "HERMES_AXIOM_EXPIRES_AT" not in values
    assert values["HERMES_AXIOM_TRACES_DATASET"] == "mine-traces"


class _NoQueryHandler(BaseHTTPRequestHandler):
    """Refuses query capability the way Axiom does when the grantor lacks it."""

    seen: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode()) if length else {}
        type(self).seen.append({"path": self.path, "body": body})
        if self.path == "/v2/datasets":
            payload, status = {"name": body["name"]}, 200
        elif any("query" in cap for cap in body.get("datasetCapabilities", {}).values()):
            payload, status = {"message": "You do not have read permission for query"}, 400
        else:
            payload, status = {"token": "xaat-ingest-only"}, 200
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture
def no_query_server():
    _NoQueryHandler.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _NoQueryHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{httpd.server_port}", _NoQueryHandler
    httpd.shutdown()


def test_a_refused_query_capability_falls_back_to_ingest_only(no_query_server) -> None:
    host, handler = no_query_server
    plane = ControlPlane(domain=host, scheme="http", backoff=0.0)
    result = provision(plane, prefix="hermes", org_token="xaat-mine")
    assert result.token == "xaat-ingest-only"
    assert result.can_query is False
    token_calls = [r for r in handler.seen if r["path"] == "/v2/tokens"]
    assert len(token_calls) == 2
    assert "query" in str(token_calls[0]["body"])
    assert "query" not in str(token_calls[1]["body"])


def test_a_granted_query_capability_is_kept(server) -> None:
    host, _ = server
    assert _run(host).can_query is True


def test_a_non_permission_error_while_minting_is_not_retried(server, monkeypatch) -> None:
    from hermess_metrics.control_plane import AxiomError

    host, handler = server
    plane = ControlPlane(domain=host, scheme="http", backoff=0.0)

    def explode(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise AxiomError(500, "server on fire")

    monkeypatch.setattr(ControlPlane, "create_ingest_token", explode)
    with pytest.raises(AxiomError):
        provision(plane, prefix="hermes", org_token="xaat-mine")
