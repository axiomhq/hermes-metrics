# SPDX-License-Identifier: Apache-2.0 OR MIT
"""``register(ctx)`` is the single entry point Hermes calls at load time."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

import hermess_metrics
from hermess_metrics.config import Config
from hermess_metrics.runtime import HOOK_KINDS


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


def test_register_on_inactive_config_observes_nothing() -> None:
    """Setup stays reachable while idle; that is how the operator fixes it."""
    ctx = FakeCtx()
    hermess_metrics.register(ctx, env={})
    assert ctx.hooks == []
    assert ctx.tools == []
    assert ctx.middleware == []


def test_register_on_active_config_subscribes_to_the_hooks() -> None:
    ctx = FakeCtx()
    hermess_metrics.register(ctx, env=FULL_ENV, runtime_factory=_FakeRuntime)
    assert [name for name, _ in ctx.hooks] == list(HOOK_KINDS) + ["on_session_finalize"]


def test_register_survives_a_runtime_that_cannot_start() -> None:
    ctx = FakeCtx()
    config = hermess_metrics.register(ctx, env=FULL_ENV, runtime_factory=_explode)
    assert config.active
    assert ctx.hooks == []
    assert ctx.cli_commands == ["axiom"]


def _explode(config: Config) -> Any:
    raise RuntimeError("no exporters here")


class _FakeRuntime:
    def __init__(self, config: Config) -> None:
        self.config = config

    def register_hooks(self, ctx: Any) -> tuple[str, ...]:
        for hook in (*HOOK_KINDS, "on_session_finalize"):
            ctx.register_hook(hook, lambda **kwargs: None)
        return (*HOOK_KINDS, "on_session_finalize")


def test_register_reports_the_posture_it_resolved() -> None:
    ctx = FakeCtx()
    cfg = hermess_metrics.register(ctx, env=FULL_ENV)
    assert isinstance(cfg, Config)
    assert cfg.active


def test_register_defaults_to_the_process_environment(monkeypatch: Any) -> None:
    for name in Config.env_names():
        monkeypatch.delenv(name, raising=False)
    assert hermess_metrics.register(FakeCtx()).active is False


# Hermes swallows a raising plugin, so a raise here is silent breakage.
@given(
    st.dictionaries(
        keys=st.sampled_from(sorted(Config.env_names())),
        values=st.text(max_size=200),
        max_size=10,
    )
)
def test_register_never_raises(env: dict[str, str]) -> None:
    hermess_metrics.register(FakeCtx(), env=env, runtime_factory=_FakeRuntime)


def test_register_stays_idle_when_the_otlp_extra_is_absent(monkeypatch: Any) -> None:
    """The command must exist here too; a drop-in install lands in exactly this state."""
    from hermess_metrics import transport

    monkeypatch.setattr(transport, "sdk_hint", lambda: "the opentelemetry SDK is required")
    ctx = FakeCtx()
    config = hermess_metrics.register(ctx, env=FULL_ENV, runtime_factory=_FakeRuntime)
    assert config.active
    assert ctx.hooks == []
    assert ctx.cli_commands == ["axiom"]


def test_the_setup_command_is_available_even_when_idle() -> None:
    ctx = FakeCtx()
    hermess_metrics.register(ctx, env={})
    assert ctx.cli_commands == ["axiom"]


def test_the_setup_command_is_available_when_configured() -> None:
    ctx = FakeCtx()
    hermess_metrics.register(ctx, env=FULL_ENV, runtime_factory=_FakeRuntime)
    assert ctx.cli_commands == ["axiom"]


def test_the_command_is_registered_however_registration_ends() -> None:
    """Whatever else fails, `hermes axiom` has to exist."""
    from hermess_metrics import transport

    cases = [
        ({}, _FakeRuntime),
        (FULL_ENV, _FakeRuntime),
        (FULL_ENV, _explode),
    ]
    for env, factory in cases:
        ctx = FakeCtx()
        hermess_metrics.register(ctx, env=env, runtime_factory=factory)
        assert ctx.cli_commands == ["axiom"], (env, factory)
    assert transport is not None
