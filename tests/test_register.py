"""``register(ctx)`` is the single entry point Hermes calls at load time."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

import hermess_metrics
from hermess_metrics.config import Config


class FakeCtx:
    """Records every registration a plugin attempts."""

    def __init__(self) -> None:
        self.hooks: list[tuple[str, Callable[..., Any]]] = []
        self.tools: list[str] = []
        self.cli_commands: list[str] = []
        self.middleware: list[str] = []

    def register_hook(self, hook_name: str, callback: Callable[..., Any]) -> None:
        self.hooks.append((hook_name, callback))

    def register_tool(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.tools.append(name)

    def register_cli_command(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.cli_commands.append(name)

    def register_middleware(self, kind: str, callback: Callable[..., Any]) -> None:
        self.middleware.append(kind)


FULL_ENV = {
    "HERMES_AXIOM_TOKEN": "xaat-secret",
    "HERMES_AXIOM_TRACES_DATASET": "hermes-traces",
}


def test_register_is_exported_at_package_level() -> None:
    assert callable(hermess_metrics.register)


def test_register_on_inactive_config_registers_nothing() -> None:
    ctx = FakeCtx()
    hermess_metrics.register(ctx, env={})
    assert ctx.hooks == []
    assert ctx.tools == []
    assert ctx.cli_commands == []
    assert ctx.middleware == []


def test_register_on_active_config_registers_nothing_yet() -> None:
    ctx = FakeCtx()
    hermess_metrics.register(ctx, env=FULL_ENV)
    assert ctx.hooks == []


def test_register_reports_the_posture_it_resolved() -> None:
    ctx = FakeCtx()
    cfg = hermess_metrics.register(ctx, env=FULL_ENV)
    assert isinstance(cfg, Config)
    assert cfg.active


def test_register_defaults_to_the_process_environment(monkeypatch: Any) -> None:
    for name in Config.env_names():
        monkeypatch.delenv(name, raising=False)
    assert hermess_metrics.register(FakeCtx()).active is False


# Hermes catches and logs whatever a plugin raises, so a raise here is silent
# breakage rather than a visible failure.
@given(
    st.dictionaries(
        keys=st.sampled_from(sorted(Config.env_names())),
        values=st.text(max_size=200),
        max_size=10,
    )
)
def test_register_never_raises(env: dict[str, str]) -> None:
    hermess_metrics.register(FakeCtx(), env=env)
