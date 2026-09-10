"""The hermes axiom subcommand."""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from hermess_metrics import cli

ORG = {
    "id": "hermes-x1",
    "name": "hermes",
    "defaultEdgeDeployment": "cloud.us-east-1.aws",
    "expiresAt": "2026-09-11T10:50:37.466Z",
    "claimUrl": "https://app.axiom.co/orgs/hermes-x1/claim?token=abc",
    "token": "xaat-full",
}


class _Handler(BaseHTTPRequestHandler):
    seen: list[dict[str, Any]] = []
    fail: bool = False

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode()) if length else None
        type(self).seen.append({"path": self.path, "body": body})
        if type(self).fail:
            payload: Any = {"message": "nope"}
            status = 403
        elif self.path == "/v2/orgs/provision":
            payload, status = ORG, 200
        elif self.path == "/v2/datasets":
            payload, status = {"name": body["name"]}, 200
        else:
            payload, status = {"token": "xaat-scoped"}, 200
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture
def server():
    _Handler.seen = []
    _Handler.fail = False
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{httpd.server_port}", _Handler
    httpd.shutdown()


def _args(host: str, target: Path, **overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "axiom_action": "setup",
        "token": None,
        "provision": False,
        "prefix": "hermes",
        "domain": host,
        "region": None,
        "org": "",
        "env_file": str(target),
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _plain_http(monkeypatch: pytest.MonkeyPatch) -> None:
    from hermess_metrics import control_plane

    original = control_plane.ControlPlane

    def http_plane(**kwargs: Any) -> Any:
        kwargs.setdefault("scheme", "http")
        kwargs.setdefault("backoff", 0.0)
        return original(**kwargs)

    monkeypatch.setattr(cli, "ControlPlane", http_plane)


class _Recorder:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str = "") -> None:
        self.lines.append(text)


def test_the_command_is_registered_with_hermes() -> None:
    registered: list[tuple[str, str]] = []

    class Ctx:
        def register_cli_command(
            self, name: str, help: str, setup_fn: Any, handler_fn: Any, **kw: Any
        ) -> None:
            registered.append((name, help))
            parser = argparse.ArgumentParser()
            setup_fn(parser)

    cli.register_cli(Ctx())
    assert registered[0][0] == "axiom"


def test_provision_flag_skips_the_question(server, tmp_path, monkeypatch) -> None:
    host, handler = server
    _plain_http(monkeypatch)
    out = _Recorder()
    code = cli.run_setup(_args(host, tmp_path / ".env", provision=True), ask=_never, out=out)
    assert code == 0
    assert handler.seen[0]["path"] == "/v2/orgs/provision"


def test_a_token_flag_adopts_that_org(server, tmp_path, monkeypatch) -> None:
    host, handler = server
    _plain_http(monkeypatch)
    code = cli.run_setup(
        _args(host, tmp_path / ".env", token="xaat-mine"), ask=_never, out=_Recorder()
    )
    assert code == 0
    assert "/v2/orgs/provision" not in [r["path"] for r in handler.seen]


def test_answering_one_provisions(server, tmp_path, monkeypatch) -> None:
    host, handler = server
    _plain_http(monkeypatch)
    code = cli.run_setup(_args(host, tmp_path / ".env"), ask=_answers(["1"]), out=_Recorder())
    assert code == 0
    assert handler.seen[0]["path"] == "/v2/orgs/provision"


def test_answering_two_asks_for_a_token(server, tmp_path, monkeypatch) -> None:
    host, handler = server
    _plain_http(monkeypatch)
    code = cli.run_setup(
        _args(host, tmp_path / ".env"), ask=_answers(["2", "xaat-mine", ""]), out=_Recorder()
    )
    assert code == 0
    assert "/v2/orgs/provision" not in [r["path"] for r in handler.seen]


def test_an_unrecognised_answer_cancels(server, tmp_path, monkeypatch) -> None:
    host, _ = server
    _plain_http(monkeypatch)
    out = _Recorder()
    assert cli.run_setup(_args(host, tmp_path / ".env"), ask=_answers(["banana"]), out=out) == 2
    assert "cancelled" in out.lines[0]


def test_an_empty_token_cancels(server, tmp_path, monkeypatch) -> None:
    host, _ = server
    _plain_http(monkeypatch)
    assert (
        cli.run_setup(_args(host, tmp_path / ".env"), ask=_answers(["2", " "]), out=_Recorder())
        == 2
    )


def test_an_interrupted_question_cancels(server, tmp_path, monkeypatch) -> None:
    host, _ = server
    _plain_http(monkeypatch)
    assert cli.run_setup(_args(host, tmp_path / ".env"), ask=_interrupt, out=_Recorder()) == 2


def test_settings_are_written_where_asked(server, tmp_path, monkeypatch) -> None:
    host, _ = server
    _plain_http(monkeypatch)
    target = tmp_path / "nested" / ".env"
    cli.run_setup(_args(host, target, provision=True), ask=_never, out=_Recorder())
    written = target.read_text()
    assert "HERMES_AXIOM_TOKEN=xaat-scoped" in written
    assert "HERMES_AXIOM_TRACES_DATASET=hermes-traces" in written


def test_the_claim_link_is_printed_for_a_new_org(server, tmp_path, monkeypatch) -> None:
    host, _ = server
    _plain_http(monkeypatch)
    out = _Recorder()
    cli.run_setup(_args(host, tmp_path / ".env", provision=True), ask=_never, out=out)
    assert any(ORG["claimUrl"] in line for line in out.lines)


def test_no_claim_link_for_an_adopted_org(server, tmp_path, monkeypatch) -> None:
    host, _ = server
    _plain_http(monkeypatch)
    out = _Recorder()
    cli.run_setup(_args(host, tmp_path / ".env", token="xaat-mine"), ask=_never, out=out)
    assert not any("Claim" in line for line in out.lines)


def test_a_failing_api_reports_and_exits_nonzero(server, tmp_path, monkeypatch) -> None:
    host, handler = server
    handler.fail = True
    _plain_http(monkeypatch)
    out = _Recorder()
    assert cli.run_setup(_args(host, tmp_path / ".env", provision=True), ask=_never, out=out) == 1
    assert "failed" in out.lines[0]


def test_status_reports_an_idle_plugin(monkeypatch) -> None:
    for name in ("HERMES_AXIOM_TOKEN", "HERMES_AXIOM_TRACES_DATASET"):
        monkeypatch.delenv(name, raising=False)
    out = _Recorder()
    assert cli.run_status(out=out) == 1
    assert "idle" in out.lines[0]


def test_status_reports_a_configured_plugin(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_AXIOM_TOKEN", "xaat-1")
    monkeypatch.setenv("HERMES_AXIOM_TRACES_DATASET", "t")
    monkeypatch.setenv("HERMES_AXIOM_ORG", "org-1")
    monkeypatch.setenv("HERMES_AXIOM_CLAIM_URL", "https://claim.me")
    monkeypatch.setenv("HERMES_AXIOM_EXPIRES_AT", "tomorrow")
    out = _Recorder()
    assert cli.run_status(out=out) == 0
    assert any("org-1" in line for line in out.lines)
    assert any("https://claim.me" in line for line in out.lines)


def test_handle_routes_to_status(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_AXIOM_TOKEN", raising=False)
    assert cli.handle(argparse.Namespace(axiom_action="status")) == 1


def test_the_default_env_file_lives_under_the_hermes_home() -> None:
    assert cli.hermes_home().name == ".hermes"


def _never(prompt: str) -> str:
    raise AssertionError("should not have asked")


def _interrupt(prompt: str) -> str:
    raise KeyboardInterrupt


def _answers(queue: list[str]):
    remaining = list(queue)

    def ask(prompt: str) -> str:
        return remaining.pop(0)

    return ask


def test_handle_routes_to_setup(server, tmp_path, monkeypatch) -> None:
    host, handler = server
    _plain_http(monkeypatch)
    monkeypatch.setattr("builtins.input", _never)
    assert cli.handle(_args(host, tmp_path / ".env", provision=True)) == 0
    assert handler.seen[0]["path"] == "/v2/orgs/provision"


class _LimitedHandler(BaseHTTPRequestHandler):
    resets_at: int = 0

    def do_POST(self) -> None:  # noqa: N802
        self.send_response(429)
        if type(self).resets_at:
            self.send_header("x-ratelimit-reset", str(type(self).resets_at))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture
def limited_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _LimitedHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_a_rate_limited_provision_explains_itself(limited_server, tmp_path, monkeypatch) -> None:
    import time as _time

    _LimitedHandler.resets_at = int(_time.time()) + 7200
    _plain_http(monkeypatch)
    out = _Recorder()
    code = cli.run_setup(
        _args(limited_server, tmp_path / ".env", provision=True), ask=_never, out=out
    )
    assert code == 1
    joined = "\n".join(out.lines)
    assert "rate limiting" in joined
    assert "UTC" in joined
    assert "--token" in joined


def test_a_rate_limit_without_a_reset_still_suggests_the_alternative(
    limited_server, tmp_path, monkeypatch
) -> None:
    _LimitedHandler.resets_at = 0
    _plain_http(monkeypatch)
    out = _Recorder()
    cli.run_setup(_args(limited_server, tmp_path / ".env", provision=True), ask=_never, out=out)
    joined = "\n".join(out.lines)
    assert "--token" in joined
    assert "resets at" not in joined


def test_a_rate_limit_while_adopting_is_reported_plainly(
    limited_server, tmp_path, monkeypatch
) -> None:
    _LimitedHandler.resets_at = 0
    _plain_http(monkeypatch)
    out = _Recorder()
    cli.run_setup(_args(limited_server, tmp_path / ".env", token="xaat-mine"), ask=_never, out=out)
    assert out.lines[0].startswith("Setup failed:")
    assert "--token" not in "\n".join(out.lines)


def test_the_org_is_asked_for_after_the_token(server, tmp_path, monkeypatch) -> None:
    host, handler = server
    _plain_http(monkeypatch)
    asked: list[str] = []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return ["2", "xaat-mine", "my-org-7"][len(asked) - 1]

    assert cli.run_setup(_args(host, tmp_path / ".env"), ask=ask, out=_Recorder()) == 0
    assert "org id" in asked[2]


def test_an_empty_org_answer_is_allowed(server, tmp_path, monkeypatch) -> None:
    host, _ = server
    _plain_http(monkeypatch)
    code = cli.run_setup(
        _args(host, tmp_path / ".env"), ask=_answers(["2", "xaat-mine", ""]), out=_Recorder()
    )
    assert code == 0


def test_the_org_flag_skips_the_org_question(server, tmp_path, monkeypatch) -> None:
    host, _ = server
    _plain_http(monkeypatch)
    code = cli.run_setup(
        _args(host, tmp_path / ".env", org="flagged-org"),
        ask=_answers(["2", "xaat-mine"]),
        out=_Recorder(),
    )
    assert code == 0


class _DeniedHandler(BaseHTTPRequestHandler):
    body: bytes = b'{"message":"forbidden"}'
    read_status: dict[str, int] = {}

    def do_POST(self) -> None:  # noqa: N802
        self._send(403, type(self).body)

    def do_GET(self) -> None:  # noqa: N802
        self._send(type(self).read_status.get(self.path, 403), b"[]")

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("x-axiom-trace-id", "trace-abc123")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture
def denied_server():
    _DeniedHandler.body = b'{"message":"forbidden"}'
    _DeniedHandler.read_status = {}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _DeniedHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{httpd.server_port}", _DeniedHandler
    httpd.shutdown()


def _deny(host: str, tmp_path: Path, out: Any, **overrides: Any) -> int:
    return cli.run_setup(
        _args(host, tmp_path / ".env", token="xaat-mine", **overrides), ask=_never, out=out
    )


def test_a_token_accepted_elsewhere_names_the_one_missing_permission(
    denied_server, tmp_path, monkeypatch
) -> None:
    """A token with apiTokens:read but no datasets permission at all."""
    host, handler = denied_server
    handler.read_status = {"/v2/tokens": 200}
    _plain_http(monkeypatch)
    out = _Recorder()
    assert _deny(host, tmp_path, out) == 1
    joined = "\n".join(out.lines)
    assert "creating dataset 'hermes-traces' was refused (403)" in joined
    assert f"needs the {cli.PERMISSION_DATASETS} permission" in joined
    assert "the token is accepted" in joined
    assert "settings/api-tokens" in joined


def test_a_token_refused_everywhere_does_not_pin_one_permission(
    denied_server, tmp_path, monkeypatch
) -> None:
    host, handler = denied_server
    handler.read_status = {}
    _plain_http(monkeypatch)
    out = _Recorder()
    assert _deny(host, tmp_path, out) == 1
    joined = "\n".join(out.lines)
    assert "Every other read with the same token was refused" in joined
    assert "missing more than this one permission" in joined


def test_the_probe_tries_more_than_datasets(denied_server, tmp_path, monkeypatch) -> None:
    """Probing only datasets would misread a token that lacks datasets:read."""
    host, handler = denied_server
    handler.read_status = {"/v2/dashboards": 200}
    _plain_http(monkeypatch)
    out = _Recorder()
    _deny(host, tmp_path, out)
    assert any("the token is accepted" in line for line in out.lines)


def test_the_axiom_trace_id_is_reported(denied_server, tmp_path, monkeypatch) -> None:
    host, _ = denied_server
    _plain_http(monkeypatch)
    out = _Recorder()
    _deny(host, tmp_path, out)
    assert any("trace-abc123" in line for line in out.lines)


def test_a_denial_with_an_org_names_that_org(denied_server, tmp_path, monkeypatch) -> None:
    host, _ = denied_server
    _plain_http(monkeypatch)
    out = _Recorder()
    _deny(host, tmp_path, out, org="my-org-7")
    assert any("'my-org-7'" in line for line in out.lines)


def test_a_detailed_denial_message_is_passed_through(denied_server, tmp_path, monkeypatch) -> None:
    host, handler = denied_server
    handler.body = b'{"message":"token does not have access to resource: datasets"}'
    _plain_http(monkeypatch)
    out = _Recorder()
    _deny(host, tmp_path, out)
    assert any("token does not have access" in line for line in out.lines)


def test_the_question_names_the_required_and_optional_permissions() -> None:
    """Only dataset creation is required; token creation just narrows the result."""
    assert cli.PERMISSION_DATASETS in cli.QUESTION
    assert cli.PERMISSION_TOKENS in cli.QUESTION
    assert "without it your token is stored as-is" in cli.QUESTION


def test_the_token_flag_help_names_both_permissions() -> None:
    parser = argparse.ArgumentParser()
    cli.build_parser(parser)
    subparser_help = [
        action.choices["setup"].format_help()
        for action in parser._actions
        if hasattr(action, "choices") and action.choices and "setup" in action.choices
    ][0]
    flat = " ".join(subparser_help.split())
    assert cli.PERMISSION_DATASETS in flat
    assert cli.PERMISSION_TOKENS in flat


def test_an_ingest_only_token_is_called_out(server, tmp_path, monkeypatch) -> None:
    from hermess_metrics import provision as provision_module

    host, _ = server
    _plain_http(monkeypatch)
    monkeypatch.setattr(
        provision_module,
        "_mint",
        lambda admin, datasets, supplied: ("xaat-ingest-only", False, True),
    )
    out = _Recorder()
    assert cli.run_setup(_args(host, tmp_path / ".env", provision=True), ask=_never, out=out) == 0
    assert any("cannot grant query access" in line for line in out.lines)


def test_keeping_the_supplied_token_is_called_out(server, tmp_path, monkeypatch) -> None:
    from hermess_metrics import provision as provision_module

    host, _ = server
    _plain_http(monkeypatch)
    monkeypatch.setattr(
        provision_module, "_mint", lambda admin, datasets, supplied: ("xaat-mine", True, False)
    )
    out = _Recorder()
    code = cli.run_setup(_args(host, tmp_path / ".env", token="xaat-mine"), ask=_never, out=out)
    assert code == 0
    joined = "\n".join(out.lines)
    assert "Stored the token you supplied" in joined
    assert cli.PERMISSION_TOKENS in joined
    assert (tmp_path / ".env").read_text().count("xaat-mine") == 1
