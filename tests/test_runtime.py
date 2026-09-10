"""Assembly of providers, recorders and hooks.

One hook invocation must produce exactly one event, reaching every recorder
that consumes its kind and no recorder twice.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from hermess_metrics.config import Config
from hermess_metrics.runtime import EXPECTED_SCHEMA, HOOK_KINDS, Runtime
from hermess_metrics.transport import Transport

ALL_ENV = {
    "HERMES_AXIOM_TOKEN": "xaat-test",
    "HERMES_AXIOM_TRACES_DATASET": "t",
    "HERMES_AXIOM_LOGS_DATASET": "l",
    "HERMES_AXIOM_METRICS_DATASET": "m",
}
SESSION = "sess-1"
TURN = "turn-1"


class FakeCtx:
    def __init__(self) -> None:
        self.hooks: dict[str, Any] = {}

    def register_hook(self, name: str, callback: Any) -> None:
        self.hooks[name] = callback


@pytest.fixture
def runtime_and_sinks(request: pytest.FixtureRequest):
    env = getattr(request, "param", ALL_ENV)
    config = Config.from_env(env)
    spans = InMemorySpanExporter()
    logs = InMemoryLogRecordExporter()
    exporters: dict[str, Any] = {}
    if config.traces_dataset:
        exporters["traces"] = spans
    if config.logs_dataset:
        exporters["logs"] = logs
    if config.metrics_dataset:
        exporters["metrics"] = _NullMetricExporter()
    runtime = Runtime.build(config, Transport(exporters=exporters, resource=Resource.create({})))
    yield runtime, spans, logs
    runtime.shutdown(2.0)


class _NullMetricExporter:
    """Accepts metric exports so the periodic reader has somewhere to go."""

    _preferred_temporality: dict[type, Any] = {}
    _preferred_aggregation: dict[type, Any] = {}

    def export(self, metrics_data: Any, timeout_millis: int = 10_000, **kwargs: Any) -> bool:
        return True

    def force_flush(self, timeout_millis: int = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: int = 30_000, **kwargs: Any) -> None:
        return None


def _api_payload() -> dict[str, Any]:
    now = time.time()
    return {
        "session_id": SESSION,
        "turn_id": TURN,
        "api_request_id": "req-1",
        "platform": "cli",
        "model": "anthropic/claude-opus-5",
        "provider": "anthropic",
        "response_model": "claude-opus-5",
        "started_at": now,
        "ended_at": now + 0.5,
        "api_duration": 0.5,
        "finish_reason": "stop",
        "usage": {"input_tokens": 10, "output_tokens": 2},
        "telemetry_schema_version": EXPECTED_SCHEMA,
    }


def test_every_documented_hook_is_registered(runtime_and_sinks) -> None:
    runtime, _, _ = runtime_and_sinks
    ctx = FakeCtx()
    registered = runtime.register_hooks(ctx)
    assert set(HOOK_KINDS) <= set(ctx.hooks)
    assert "on_session_finalize" in ctx.hooks
    assert set(registered) == set(ctx.hooks)


def test_one_hook_call_reaches_traces_and_metrics_once(runtime_and_sinks) -> None:
    runtime, spans, _ = runtime_and_sinks
    ctx = FakeCtx()
    runtime.register_hooks(ctx)
    ctx.hooks["post_api_request"](**_api_payload())
    assert runtime.flush(5.0)
    assert runtime.dispatcher.stats().accepted == 1
    assert [span.name for span in spans.get_finished_spans()] == ["chat claude-opus-5"]


def test_a_provider_error_reaches_all_three_signals(runtime_and_sinks) -> None:
    runtime, spans, logs = runtime_and_sinks
    ctx = FakeCtx()
    runtime.register_hooks(ctx)
    payload = _api_payload()
    payload.update(status_code=429, reason="rate_limited", retryable=True, error="429")
    ctx.hooks["api_request_error"](**payload)
    assert runtime.flush(5.0)
    assert len(spans.get_finished_spans()) == 1
    assert len(logs.get_finished_logs()) == 1


@pytest.mark.parametrize(
    "runtime_and_sinks",
    [{"HERMES_AXIOM_TOKEN": "t", "HERMES_AXIOM_TRACES_DATASET": "t"}],
    indirect=True,
)
def test_only_configured_signals_get_a_recorder(runtime_and_sinks) -> None:
    runtime, _, _ = runtime_and_sinks
    assert len(runtime.recorders) == 1
    assert len(runtime.providers) == 1


def test_a_changed_payload_schema_is_reported_once(
    runtime_and_sinks, caplog: pytest.LogCaptureFixture
) -> None:
    runtime, _, logs = runtime_and_sinks
    ctx = FakeCtx()
    runtime.register_hooks(ctx)
    payload = _api_payload()
    payload["telemetry_schema_version"] = "hermes.observer.v2"
    with caplog.at_level(logging.WARNING):
        ctx.hooks["post_api_request"](**payload)
        ctx.hooks["post_api_request"](**payload)
    assert runtime.flush(5.0)
    assert sum("schema" in record.getMessage() for record in caplog.records) == 1
    bodies = [item.log_record.body for item in logs.get_finished_logs()]
    assert bodies.count("hook payload schema changed") == 1


def test_the_expected_schema_passes_without_complaint(
    runtime_and_sinks, caplog: pytest.LogCaptureFixture
) -> None:
    runtime, _, logs = runtime_and_sinks
    ctx = FakeCtx()
    runtime.register_hooks(ctx)
    with caplog.at_level(logging.WARNING):
        ctx.hooks["post_api_request"](**_api_payload())
    assert runtime.flush(5.0)
    assert not any("schema" in record.getMessage() for record in caplog.records)


def test_the_finalize_hook_flushes(runtime_and_sinks) -> None:
    runtime, spans, _ = runtime_and_sinks
    ctx = FakeCtx()
    runtime.register_hooks(ctx)
    ctx.hooks["post_api_request"](**_api_payload())
    ctx.hooks["on_session_finalize"](session_id=SESSION, platform="cli")
    assert len(spans.get_finished_spans()) == 1


def test_shutdown_is_idempotent(runtime_and_sinks) -> None:
    runtime, _, _ = runtime_and_sinks
    runtime.shutdown(2.0)
    runtime.shutdown(2.0)


def test_hook_callbacks_never_raise(runtime_and_sinks) -> None:
    """Hermes swallows a raising callback into a warning nobody reads."""
    runtime, _, _ = runtime_and_sinks
    ctx = FakeCtx()
    runtime.register_hooks(ctx)
    for callback in ctx.hooks.values():
        callback()
        callback(session_id=None, turn_id=object(), usage="not a mapping")
    assert runtime.flush(5.0)


def test_start_builds_real_exporters_from_config() -> None:
    runtime = Runtime.start(Config.from_env(ALL_ENV))
    try:
        assert len(runtime.recorders) == 6
        assert len(runtime.providers) == 3
    finally:
        runtime.shutdown(2.0)


def test_plugin_health_is_published_alongside_the_metrics(runtime_and_sinks) -> None:
    runtime, _, _ = runtime_and_sinks
    ctx = FakeCtx()
    runtime.register_hooks(ctx)
    ctx.hooks["post_api_request"](**_api_payload())
    assert runtime.flush(5.0)
    assert runtime.dispatcher.stats().accepted == 1
    assert runtime.dispatcher.depth == 0
