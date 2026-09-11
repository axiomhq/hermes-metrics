# SPDX-License-Identifier: Apache-2.0 OR MIT
"""The hermes axiom subcommand."""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from hermes_metrics import cli

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
    script: dict[str, tuple[int, Any]] = {}

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode()) if length else None
        type(self).seen.append({"path": self.path, "body": body})
        if self.path in type(self).script:
            payload, status = type(self).script[self.path][1], type(self).script[self.path][0]
        elif type(self).fail:
            payload, status = {"message": "nope"}, 403
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
    _Handler.script = {}
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
        "no_alerts": True,
        "no_dashboard": True,
        "tokens_per_15m": None,
        "spend_per_hour": None,
        "max_subagents": None,
        "defaults": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _plain_http(monkeypatch: pytest.MonkeyPatch) -> None:
    from hermes_metrics import control_plane

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
    from hermes_metrics import provision as provision_module

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
    from hermes_metrics import provision as provision_module

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


def test_status_reports_a_missing_sdk_rather_than_claiming_it_is_on(monkeypatch) -> None:
    """A drop-in install copies the plugin but not opentelemetry."""
    from hermes_metrics import transport

    monkeypatch.setenv("HERMES_AXIOM_TOKEN", "xaat-1")
    monkeypatch.setenv("HERMES_AXIOM_TRACES_DATASET", "t")
    monkeypatch.setattr(transport, "sdk_hint", lambda: "the opentelemetry SDK is required")
    out = _Recorder()
    assert cli.run_status(out=out) == 1
    joined = "\n".join(out.lines)
    assert "cannot export" in joined
    assert "opentelemetry" in joined
    assert "is on" not in joined


def test_status_names_both_problems_when_both_apply(monkeypatch) -> None:
    from hermes_metrics import transport

    for name in ("HERMES_AXIOM_TOKEN", "HERMES_AXIOM_TRACES_DATASET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(transport, "sdk_hint", lambda: "the opentelemetry SDK is required")
    out = _Recorder()
    assert cli.run_status(out=out) == 1
    joined = "\n".join(out.lines)
    assert "cannot export" in joined
    assert "Settings are missing too" in joined


def _alert_args(host: str, **overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "axiom_action": "alerts",
        "token": None,
        "org": "",
        "domain": host,
        "tokens_per_15m": None,
        "spend_per_hour": None,
        "max_subagents": None,
        "defaults": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_alerts_without_setup_says_so(monkeypatch) -> None:
    for name in (
        "HERMES_AXIOM_TRACES_DATASET",
        "HERMES_AXIOM_LOGS_DATASET",
        "HERMES_AXIOM_METRICS_DATASET",
    ):
        monkeypatch.delenv(name, raising=False)
    out = _Recorder()
    assert cli.run_alerts(_alert_args("x"), out=out) == 1
    assert "setup" in out.lines[0]


def test_alerts_reports_each_monitor(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_AXIOM_TOKEN", "xaat-1")
    monkeypatch.setenv("HERMES_AXIOM_TRACES_DATASET", "hermes-traces")
    monkeypatch.setenv("HERMES_AXIOM_METRICS_DATASET", "hermes-metrics")
    created: list[str] = []

    def fake_create(plane: Any, datasets: dict[str, str], budgets: Any = None) -> Any:
        created.append("called")
        return cli.alerts.Report(created=["one"], failed=[("two", "nope")])

    monkeypatch.setattr(cli.alerts, "create", fake_create)
    out = _Recorder()
    assert cli.run_alerts(_alert_args("x"), out=out) == 0
    joined = "\n".join(out.lines)
    assert "Created 1 of 2" in joined
    assert "ok      one" in joined
    assert "skipped two" in joined
    assert "nope" in joined


def test_alerts_exits_nonzero_when_nothing_was_created(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_AXIOM_TOKEN", "xaat-1")
    monkeypatch.setenv("HERMES_AXIOM_TRACES_DATASET", "hermes-traces")
    monkeypatch.setattr(
        cli.alerts,
        "create",
        lambda plane, datasets, budgets=None: cli.alerts.Report(failed=[("a", "b")]),
    )
    assert cli.run_alerts(_alert_args("x"), out=_Recorder()) == 1


def test_handle_routes_to_alerts(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_AXIOM_TRACES_DATASET", raising=False)
    monkeypatch.delenv("HERMES_AXIOM_LOGS_DATASET", raising=False)
    monkeypatch.delenv("HERMES_AXIOM_METRICS_DATASET", raising=False)
    assert cli.handle(_alert_args("x")) == 1


def _budget_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("HERMES_AXIOM_TOKEN", "xaat-1")
    monkeypatch.setenv("HERMES_AXIOM_METRICS_DATASET", "hermes-metrics")


def _capture_budgets(monkeypatch: Any) -> list[Any]:
    seen: list[Any] = []

    def fake(plane: Any, datasets: dict[str, str], budgets: Any = None) -> Any:
        seen.append(budgets)
        return cli.alerts.Report(created=["one"])

    monkeypatch.setattr(cli.alerts, "create", fake)
    return seen


def test_alerts_asks_for_each_budget_and_shows_the_default(monkeypatch) -> None:
    _budget_env(monkeypatch)
    seen = _capture_budgets(monkeypatch)
    asked: list[str] = []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return ""

    assert cli.run_alerts(_alert_args("x", defaults=False), ask=ask, out=_Recorder()) == 0
    assert len(asked) == 3
    assert "500,000" in asked[0] and "5.00" in asked[1] and "8" in asked[2]
    assert seen[0] == cli.alerts.Budgets()


def test_a_typed_budget_is_used(monkeypatch) -> None:
    _budget_env(monkeypatch)
    seen = _capture_budgets(monkeypatch)
    answers = iter(["1,200,000", "$12.50", "4"])
    cli.run_alerts(_alert_args("x", defaults=False), ask=lambda p: next(answers), out=_Recorder())
    assert seen[0].tokens_per_15m == 1_200_000
    assert seen[0].spend_per_hour == 12.5
    assert seen[0].max_subagents == 4


def test_flags_skip_the_question(monkeypatch) -> None:
    _budget_env(monkeypatch)
    seen = _capture_budgets(monkeypatch)
    args = _alert_args("x", defaults=False, tokens_per_15m=99, spend_per_hour=1, max_subagents=2)
    assert cli.run_alerts(args, ask=_never, out=_Recorder()) == 0
    assert seen[0].tokens_per_15m == 99


def test_defaults_flag_asks_nothing(monkeypatch) -> None:
    _budget_env(monkeypatch)
    seen = _capture_budgets(monkeypatch)
    assert cli.run_alerts(_alert_args("x"), ask=_never, out=_Recorder()) == 0
    assert seen[0] == cli.alerts.Budgets()


@pytest.mark.parametrize("answer", ["banana", "0", "-5"])
def test_a_nonsense_budget_cancels(monkeypatch, answer: str) -> None:
    _budget_env(monkeypatch)
    _capture_budgets(monkeypatch)
    out = _Recorder()
    assert cli.run_alerts(_alert_args("x", defaults=False), ask=lambda p: answer, out=out) == 2
    assert "cancelled" in out.lines[0]


def test_the_console_hint_is_printed_after_success(monkeypatch) -> None:
    _budget_env(monkeypatch)
    _capture_budgets(monkeypatch)
    out = _Recorder()
    cli.run_alerts(_alert_args("x"), ask=_never, out=out)
    assert any("Axiom console" in line for line in out.lines)


def test_the_chosen_budget_reaches_the_monitor(monkeypatch) -> None:
    from hermes_metrics import alerts as alerts_module

    specs = alerts_module.pack(
        {"metrics": "hermes-metrics"}, alerts_module.Budgets(tokens_per_15m=123, spend_per_hour=4.5)
    )
    by_name = {s["name"]: s for s in specs}
    assert by_name["Hermes token use is high"]["threshold"] == 123
    assert "123" in by_name["Hermes token use is high"]["description"]
    assert by_name["Hermes spend is high"]["threshold"] == 4.5


def _dash_args(host: str, **overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "axiom_action": "dashboard",
        "token": None,
        "org": "",
        "domain": host,
        "name": "",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_dashboard_without_setup_says_so(monkeypatch) -> None:
    for name in (
        "HERMES_AXIOM_TRACES_DATASET",
        "HERMES_AXIOM_LOGS_DATASET",
        "HERMES_AXIOM_METRICS_DATASET",
    ):
        monkeypatch.delenv(name, raising=False)
    out = _Recorder()
    assert cli.run_dashboard(_dash_args("x"), out=out) == 1
    assert "setup" in out.lines[0]


def test_dashboard_needs_all_three_signals(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_AXIOM_TOKEN", "xaat-1")
    monkeypatch.setenv("HERMES_AXIOM_METRICS_DATASET", "m")
    for name in ("HERMES_AXIOM_TRACES_DATASET", "HERMES_AXIOM_LOGS_DATASET"):
        monkeypatch.delenv(name, raising=False)
    out = _Recorder()
    assert cli.run_dashboard(_dash_args("x"), out=out) == 1
    assert "traces" in out.lines[0] and "logs" in out.lines[0]


def _configured(monkeypatch: Any) -> None:
    monkeypatch.setenv("HERMES_AXIOM_TOKEN", "xaat-1")
    monkeypatch.setenv("HERMES_AXIOM_METRICS_DATASET", "m")
    monkeypatch.setenv("HERMES_AXIOM_TRACES_DATASET", "t")
    monkeypatch.setenv("HERMES_AXIOM_LOGS_DATASET", "l")
    monkeypatch.setenv("HERMES_AXIOM_ORG", "org-1")


def test_dashboard_reports_the_link(server, monkeypatch) -> None:
    host, handler = server
    handler.script["/v2/dashboards"] = (200, {"dashboard": {"uid": "abc123"}})
    _plain_http(monkeypatch)
    _configured(monkeypatch)
    out = _Recorder()
    assert cli.run_dashboard(_dash_args(host), out=out) == 0
    joined = "\n".join(out.lines)
    assert "panels" in joined
    assert "app.axiom.co/org-1/dashboards/abc123" in joined


def test_dashboard_substitutes_this_installs_datasets(server, monkeypatch) -> None:
    host, handler = server
    handler.script["/v2/dashboards"] = (200, {"dashboard": {"uid": "abc"}})
    _plain_http(monkeypatch)
    _configured(monkeypatch)
    cli.run_dashboard(_dash_args(host), out=_Recorder())
    sent = json.dumps(handler.seen[0]["body"])
    assert "{{" not in sent
    assert "`m`:" in sent


def test_a_refused_dashboard_is_reported(server, monkeypatch) -> None:
    host, handler = server
    handler.script["/v2/dashboards"] = (403, {"message": "nope"})
    _plain_http(monkeypatch)
    _configured(monkeypatch)
    out = _Recorder()
    assert cli.run_dashboard(_dash_args(host), out=out) == 1
    assert "Could not create" in out.lines[0]


def test_handle_routes_to_dashboard(monkeypatch) -> None:
    for name in (
        "HERMES_AXIOM_TRACES_DATASET",
        "HERMES_AXIOM_LOGS_DATASET",
        "HERMES_AXIOM_METRICS_DATASET",
    ):
        monkeypatch.delenv(name, raising=False)
    assert cli.handle(_dash_args("x")) == 1


def test_dashboard_reports_a_bare_id_without_an_org(server, monkeypatch) -> None:
    host, handler = server
    handler.script["/v2/dashboards"] = (200, {"id": "bare-id"})
    _plain_http(monkeypatch)
    _configured(monkeypatch)
    monkeypatch.delenv("HERMES_AXIOM_ORG", raising=False)
    out = _Recorder()
    assert cli.run_dashboard(_dash_args(host), out=out) == 0
    assert any(line.strip() == "bare-id" for line in out.lines)


def _full_setup_args(host: str, target: Path, **overrides: Any) -> argparse.Namespace:
    args = _args(host, target, provision=True, **overrides)
    args.no_alerts = False
    args.no_dashboard = False
    return args


def test_setup_also_creates_the_monitors_and_the_dashboard(server, tmp_path, monkeypatch) -> None:
    """One command, no follow-up."""
    host, handler = server
    handler.script["/v2/monitors"] = (200, {"id": "m"})
    handler.script["/v2/dashboards"] = (200, {"dashboard": {"uid": "board-1"}})
    _plain_http(monkeypatch)
    out = _Recorder()
    assert cli.run_setup(_full_setup_args(host, tmp_path / ".env"), ask=_never, out=out) == 0
    joined = "\n".join(out.lines)
    assert "monitors 7 created" in joined
    assert "dashboard 24 panels" in joined
    assert "dashboards/board-1" in joined
    assert "Claim the org" in joined


def test_setup_survives_a_token_that_cannot_alert(server, tmp_path, monkeypatch) -> None:
    host, handler = server
    handler.script["/v2/monitors"] = (403, {"message": "no monitors for you"})
    handler.script["/v2/dashboards"] = (403, {"message": "no dashboards either"})
    _plain_http(monkeypatch)
    out = _Recorder()
    assert cli.run_setup(_full_setup_args(host, tmp_path / ".env"), ask=_never, out=out) == 0
    joined = "\n".join(out.lines)
    assert "monitors skipped" in joined
    assert "dashboard skipped" in joined
    assert "Axiom telemetry is configured" in joined


def test_setup_extras_can_be_skipped(server, tmp_path, monkeypatch) -> None:
    host, handler = server
    _plain_http(monkeypatch)
    out = _Recorder()
    cli.run_setup(_args(host, tmp_path / ".env", provision=True), ask=_never, out=out)
    paths = [r["path"] for r in handler.seen]
    assert "/v2/monitors" not in paths
    assert "/v2/dashboards" not in paths


def test_setup_uses_the_token_it_persisted(server, tmp_path, monkeypatch) -> None:
    """Whatever setup proves here is what the operator is left holding."""
    host, handler = server
    handler.script["/v2/monitors"] = (200, {"id": "m"})
    handler.script["/v2/dashboards"] = (200, {"dashboard": {"uid": "b"}})
    _plain_http(monkeypatch)
    cli.run_setup(_full_setup_args(host, tmp_path / ".env"), ask=_never, out=_Recorder())
    written = (tmp_path / ".env").read_text()
    assert "HERMES_AXIOM_TOKEN=xaat-scoped" in written


def test_setup_asks_for_the_budgets_before_creating_anything(server, tmp_path, monkeypatch) -> None:
    """A cancelled answer must not leave a half-built org behind."""
    host, handler = server
    _plain_http(monkeypatch)
    args = _full_setup_args(host, tmp_path / ".env")
    args.defaults = False
    out = _Recorder()
    assert cli.run_setup(args, ask=lambda p: "banana", out=out) == 2
    assert handler.seen == []
    assert "cancelled" in out.lines[-1]


def test_setup_budget_answers_reach_the_monitors(server, tmp_path, monkeypatch) -> None:
    host, handler = server
    handler.script["/v2/monitors"] = (200, {"id": "m"})
    handler.script["/v2/dashboards"] = (200, {"dashboard": {"uid": "b"}})
    _plain_http(monkeypatch)
    seen: list[Any] = []
    real = cli.alerts.create

    def spy(plane: Any, datasets: dict[str, str], budgets: Any = None) -> Any:
        seen.append(budgets)
        return real(plane, datasets, budgets)

    monkeypatch.setattr(cli.alerts, "create", spy)
    args = _full_setup_args(host, tmp_path / ".env")
    args.defaults = False
    answers = iter(["250000", "2.50", "3"])
    assert cli.run_setup(args, ask=lambda p: next(answers), out=_Recorder()) == 0
    assert seen[0].tokens_per_15m == 250_000
    assert seen[0].spend_per_hour == 2.5
    sent = [r["body"] for r in handler.seen if r["path"] == "/v2/monitors"]
    thresholds = {b["name"]: b["threshold"] for b in sent}
    assert thresholds["Hermes token use is high"] == 250_000
    assert thresholds["Hermes spend is high"] == 2.5


def test_setup_skipping_alerts_asks_no_budget_questions(server, tmp_path, monkeypatch) -> None:
    host, _ = server
    _plain_http(monkeypatch)
    args = _args(host, tmp_path / ".env", provision=True)
    args.defaults = False
    assert cli.run_setup(args, ask=_never, out=_Recorder()) == 0
