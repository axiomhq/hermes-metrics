"""Spans built from Hermes hook payloads.

Axiom recognises AI spans by their gen_ai attributes, so the shapes here follow
Axiom's manual instrumentation conventions rather than an invented schema.
Every span is built on the worker thread from timestamps the hooks supply, so
the recorder never depends on when it happens to run.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from hermess_metrics.dispatch import Dispatcher
from hermess_metrics.events import stamp
from hermess_metrics.redaction import LEVEL_FULL, LEVEL_METADATA, MASK, Redactor
from hermess_metrics.traces import TraceRecorder


def send(dispatcher: Dispatcher[object], kind: str, **payload: Any) -> None:
    assert dispatcher.submit(stamp(kind, payload))


FLUSH = 5.0
SESSION = "sess-1"
TURN = "turn-1"


@pytest.fixture
def recorder_and_spans(request: pytest.FixtureRequest):
    level = getattr(request, "param", LEVEL_METADATA)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    dispatcher: Dispatcher[object] = Dispatcher(capacity=256)
    recorder = TraceRecorder(
        tracer=provider.get_tracer("hermess-metrics"),
        redactor=Redactor.for_level(level),
    )
    dispatcher.start(recorder.handle)
    yield recorder, exporter, dispatcher
    dispatcher.stop(FLUSH)


def _drain(dispatcher: Dispatcher[object], exporter: InMemorySpanExporter):
    assert dispatcher.flush(FLUSH)
    return {span.name: span for span in exporter.get_finished_spans()}


def _api_request(**overrides: object) -> dict[str, object]:
    now = time.time()
    payload: dict[str, object] = {
        "session_id": SESSION,
        "turn_id": TURN,
        "api_request_id": "req-1",
        "platform": "cli",
        "model": "anthropic/claude-opus-5",
        "provider": "anthropic",
        "api_duration": 1234.0,
        "started_at": now,
        "ended_at": now + 1.234,
        "finish_reason": "stop",
        "response_model": "claude-opus-5",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 7,
            "cache_write_tokens": 3,
            "reasoning_tokens": 5,
            "request_count": 1,
        },
        "assistant_message": "hello there",
        "assistant_content_chars": 11,
        "assistant_tool_call_count": 0,
    }
    payload.update(overrides)
    return payload


def test_a_session_opens_and_closes_one_agent_span(recorder_and_spans) -> None:
    recorder, exporter, dispatcher = recorder_and_spans
    send(dispatcher, "session_start", session_id=SESSION, model="m", platform="cli")
    send(
        dispatcher,
        "session_end",
        session_id=SESSION,
        completed=True,
        interrupted=False,
        platform="cli",
    )
    spans = _drain(dispatcher, exporter)
    agent = spans["invoke_agent hermes"]
    assert agent.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert agent.attributes["gen_ai.conversation.id"] == SESSION


def test_an_api_request_becomes_a_chat_span_named_for_the_model(recorder_and_spans) -> None:
    recorder, exporter, dispatcher = recorder_and_spans
    send(dispatcher, "api_request", **_api_request())
    spans = _drain(dispatcher, exporter)
    chat = spans["chat claude-opus-5"]
    assert chat.attributes["gen_ai.operation.name"] == "chat"
    assert chat.attributes["gen_ai.provider.name"] == "anthropic"
    assert chat.attributes["gen_ai.response.model"] == "claude-opus-5"
    assert chat.attributes["gen_ai.response.finish_reasons"] == ("stop",)


def test_token_usage_lands_on_the_chat_span(recorder_and_spans) -> None:
    recorder, exporter, dispatcher = recorder_and_spans
    send(dispatcher, "api_request", **_api_request())
    chat = _drain(dispatcher, exporter)["chat claude-opus-5"]
    assert chat.attributes["gen_ai.usage.input_tokens"] == 100
    assert chat.attributes["gen_ai.usage.output_tokens"] == 20
    assert chat.attributes["gen_ai.usage.cache_read_tokens"] == 7
    assert chat.attributes["gen_ai.usage.reasoning_tokens"] == 5


def test_the_chat_span_uses_the_duration_the_hook_reported(recorder_and_spans) -> None:
    recorder, exporter, dispatcher = recorder_and_spans
    send(dispatcher, "api_request", **_api_request())
    chat = _drain(dispatcher, exporter)["chat claude-opus-5"]
    elapsed_ms = (chat.end_time - chat.start_time) / 1_000_000
    assert 1230 < elapsed_ms < 1240


def test_axiom_required_attributes_are_present(recorder_and_spans) -> None:
    recorder, exporter, dispatcher = recorder_and_spans
    send(dispatcher, "api_request", **_api_request())
    chat = _drain(dispatcher, exporter)["chat claude-opus-5"]
    for name in ("gen_ai.operation.name", "gen_ai.capability.name", "gen_ai.step.name"):
        assert chat.attributes[name]
    assert chat.attributes["axiom.gen_ai.sdk.name"] == "hermess-metrics"
    assert chat.attributes["axiom.gen_ai.schema_url"].startswith("https://axiom.co/ai/schemas/")


def test_a_tool_call_becomes_an_execute_tool_span(recorder_and_spans) -> None:
    recorder, exporter, dispatcher = recorder_and_spans
    send(
        dispatcher,
        "tool_call",
        tool_name="shell_exec",
        args={"command": "ls"},
        result="a b c",
        session_id=SESSION,
        turn_id=TURN,
        tool_call_id="call-1",
        duration_ms=42.0,
        status="ok",
        error_type=None,
        error_message=None,
    )
    tool = _drain(dispatcher, exporter)["execute_tool shell_exec"]
    assert tool.attributes["gen_ai.operation.name"] == "execute_tool"
    assert tool.attributes["gen_ai.tool.name"] == "shell_exec"
    assert tool.attributes["gen_ai.tool.call.id"] == "call-1"


def test_a_failed_tool_call_is_marked_as_an_error(recorder_and_spans) -> None:
    recorder, exporter, dispatcher = recorder_and_spans
    send(
        dispatcher,
        "tool_call",
        tool_name="shell_exec",
        args={},
        result="",
        session_id=SESSION,
        turn_id=TURN,
        tool_call_id="call-2",
        duration_ms=1.0,
        status="error",
        error_type="TimeoutError",
        error_message="took too long",
    )
    tool = _drain(dispatcher, exporter)["execute_tool shell_exec"]
    assert tool.status.is_ok is False
    assert tool.attributes["error.type"] == "TimeoutError"


def test_a_provider_error_becomes_a_chat_span_marked_failed(recorder_and_spans) -> None:
    recorder, exporter, dispatcher = recorder_and_spans
    now = time.time()
    send(
        dispatcher,
        "api_error",
        session_id=SESSION,
        turn_id=TURN,
        api_request_id="req-2",
        platform="cli",
        model="anthropic/claude-opus-5",
        provider="anthropic",
        api_duration=90.0,
        started_at=now,
        ended_at=now + 0.09,
        status_code=429,
        retry_count=1,
        max_retries=3,
        retryable=True,
        reason="rate_limited",
        error="429 Too Many Requests",
    )
    chat = _drain(dispatcher, exporter)["chat claude-opus-5"]
    assert chat.status.is_ok is False
    assert chat.attributes["error.type"] == "rate_limited"
    assert chat.attributes["http.response.status_code"] == 429
    assert chat.attributes["hermes.retry_count"] == 1


def test_chat_and_tool_spans_join_the_session_trace(recorder_and_spans) -> None:
    recorder, exporter, dispatcher = recorder_and_spans
    send(dispatcher, "session_start", session_id=SESSION, model="m", platform="cli")
    send(dispatcher, "turn_start", session_id=SESSION, turn_id=TURN, model="m", platform="cli")
    send(dispatcher, "api_request", **_api_request())
    send(dispatcher, "turn_end", session_id=SESSION, turn_id=TURN, model="m", platform="cli")
    send(
        dispatcher,
        "session_end",
        session_id=SESSION,
        completed=True,
        interrupted=False,
        platform="cli",
    )
    spans = _drain(dispatcher, exporter)
    agent = spans["invoke_agent hermes"]
    turn = spans["turn"]
    chat = spans["chat claude-opus-5"]
    assert turn.parent.span_id == agent.context.span_id
    assert chat.parent.span_id == turn.context.span_id
    assert chat.context.trace_id == agent.context.trace_id


def test_the_metadata_level_keeps_prompts_and_tool_io_off_the_span(recorder_and_spans) -> None:
    recorder, exporter, dispatcher = recorder_and_spans
    send(dispatcher, "api_request", **_api_request())
    send(
        dispatcher,
        "tool_call",
        tool_name="shell_exec",
        args={"command": "cat /etc/passwd"},
        result="root:x:0:0",
        session_id=SESSION,
        turn_id=TURN,
        tool_call_id="call-3",
        duration_ms=1.0,
        status="ok",
        error_type=None,
        error_message=None,
    )
    spans = _drain(dispatcher, exporter)
    assert "gen_ai.output.messages" not in spans["chat claude-opus-5"].attributes
    assert "gen_ai.tool.arguments" not in spans["execute_tool shell_exec"].attributes


@pytest.mark.parametrize("recorder_and_spans", [LEVEL_FULL], indirect=True)
def test_the_full_level_carries_content_with_credentials_masked(recorder_and_spans) -> None:
    recorder, exporter, dispatcher = recorder_and_spans
    send(
        dispatcher,
        "tool_call",
        tool_name="http_get",
        args={"url": "https://x", "api_key": "sk-live-secret"},
        result="ok",
        session_id=SESSION,
        turn_id=TURN,
        tool_call_id="call-4",
        duration_ms=1.0,
        status="ok",
        error_type=None,
        error_message=None,
    )
    tool = _drain(dispatcher, exporter)["execute_tool http_get"]
    assert "https://x" in tool.attributes["gen_ai.tool.arguments"]
    assert "sk-live-secret" not in tool.attributes["gen_ai.tool.arguments"]
    assert MASK in tool.attributes["gen_ai.tool.arguments"]


def test_a_hook_missing_every_optional_field_still_produces_a_span(recorder_and_spans) -> None:
    recorder, exporter, dispatcher = recorder_and_spans
    send(dispatcher, "api_request", session_id=SESSION)
    spans = _drain(dispatcher, exporter)
    assert any(name.startswith("chat") for name in spans)


def test_live_span_state_is_bounded(recorder_and_spans) -> None:
    recorder, exporter, dispatcher = recorder_and_spans
    for index in range(recorder.max_live * 3):
        send(dispatcher, "session_start", session_id=f"s-{index}", model="m", platform="cli")
        if index % 64 == 0:
            assert dispatcher.flush(FLUSH)
    assert dispatcher.flush(FLUSH)
    assert recorder.live_session_count <= recorder.max_live


def test_a_foreign_queue_item_is_ignored(recorder_and_spans) -> None:
    recorder, exporter, dispatcher = recorder_and_spans
    assert dispatcher.submit("not an event")
    assert dispatcher.flush(FLUSH)
    assert dispatcher.stats().failed == 0
    assert exporter.get_finished_spans() == ()


def test_ending_a_session_that_never_started_is_harmless(recorder_and_spans) -> None:
    recorder, exporter, dispatcher = recorder_and_spans
    send(dispatcher, "session_end", session_id="never-seen", completed=True, interrupted=False)
    assert dispatcher.flush(FLUSH)
    assert exporter.get_finished_spans() == ()


def test_an_interrupted_session_is_marked(recorder_and_spans) -> None:
    recorder, exporter, dispatcher = recorder_and_spans
    send(dispatcher, "session_start", session_id=SESSION, model="m", platform="cli")
    send(
        dispatcher,
        "session_end",
        session_id=SESSION,
        completed=False,
        interrupted=True,
        platform="cli",
    )
    agent = _drain(dispatcher, exporter)["invoke_agent hermes"]
    assert agent.attributes["hermes.interrupted"] is True


@pytest.mark.parametrize("recorder_and_spans", [LEVEL_FULL], indirect=True)
def test_the_full_level_carries_the_model_output(recorder_and_spans) -> None:
    recorder, exporter, dispatcher = recorder_and_spans
    send(dispatcher, "api_request", **_api_request())
    chat = _drain(dispatcher, exporter)["chat claude-opus-5"]
    assert chat.attributes["gen_ai.output.messages"] == "hello there"


def test_structural_spans_start_when_the_hook_fired_not_when_the_worker_ran() -> None:
    """A queue backlog must not make a parent span start after its children."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    dispatcher: Dispatcher[object] = Dispatcher(capacity=32)
    recorder = TraceRecorder(
        tracer=provider.get_tracer("hermess-metrics"),
        redactor=Redactor.for_level(LEVEL_METADATA),
    )
    release = threading.Event()

    def gated(item: object) -> None:
        release.wait(FLUSH)
        recorder.handle(item)

    dispatcher.start(gated)
    try:
        hook_fired_at = time.time()
        send(dispatcher, "session_start", session_id=SESSION, model="m", platform="cli")
        send(dispatcher, "turn_start", session_id=SESSION, turn_id=TURN, model="m", platform="cli")
        time.sleep(0.3)
        release.set()
        send(dispatcher, "turn_end", session_id=SESSION, turn_id=TURN, model="m", platform="cli")
        send(dispatcher, "session_end", session_id=SESSION, completed=True, interrupted=False)
        spans = _drain(dispatcher, exporter)
    finally:
        release.set()
        dispatcher.stop(FLUSH)

    for name in ("invoke_agent hermes", "turn"):
        lag_ms = (spans[name].start_time / 1_000_000_000) - hook_fired_at
        assert lag_ms < 0.1, f"{name} started {lag_ms:.3f}s after the hook fired"


def test_the_billing_route_lands_on_the_chat_span(recorder_and_spans) -> None:
    recorder, exporter, dispatcher = recorder_and_spans
    send(dispatcher, "api_request", **_api_request())
    chat = _drain(dispatcher, exporter)["chat claude-opus-5"]
    assert chat.attributes["hermes.billing_mode"] == "official_docs_snapshot"


def test_an_event_kind_this_recorder_does_not_consume_is_ignored(recorder_and_spans) -> None:
    recorder, exporter, dispatcher = recorder_and_spans
    assert dispatcher.submit(stamp("diagnostic", {"message": "for someone else"}))
    assert dispatcher.flush(FLUSH)
    assert dispatcher.stats().failed == 0
    assert exporter.get_finished_spans() == ()
