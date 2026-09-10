"""Counters and histograms built from hook payloads."""

from __future__ import annotations

import time
from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from hermess_metrics.dispatch import Dispatcher
from hermess_metrics.events import stamp
from hermess_metrics.metrics import MetricRecorder


def send(dispatcher: Dispatcher[object], kind: str, **payload: Any) -> None:
    assert dispatcher.submit(stamp(kind, payload))


FLUSH = 5.0
SESSION = "sess-cardinality-canary"
TURN = "turn-cardinality-canary"
REQUEST = "req-cardinality-canary"
CALL = "call-cardinality-canary"


@pytest.fixture
def recorder_and_reader():
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    dispatcher: Dispatcher[object] = Dispatcher(capacity=256)
    recorder = MetricRecorder(meter=provider.get_meter("hermess-metrics"))
    dispatcher.start(recorder.handle)
    yield recorder, reader, dispatcher
    dispatcher.stop(FLUSH)


def _points(reader: InMemoryMetricReader) -> dict[str, list[Any]]:
    data = reader.get_metrics_data()
    found: dict[str, list[Any]] = {}
    for resource_metric in data.resource_metrics if data else []:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                found.setdefault(metric.name, []).extend(metric.data.data_points)
    return found


def _api_request(**overrides: Any) -> dict[str, Any]:
    now = time.time()
    payload: dict[str, Any] = {
        "session_id": SESSION,
        "turn_id": TURN,
        "api_request_id": REQUEST,
        "platform": "cli",
        "model": "anthropic/claude-opus-5",
        "provider": "anthropic",
        "api_duration": 0.87,
        "started_at": now,
        "ended_at": now + 0.87,
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
    }
    payload.update(overrides)
    return payload


def test_an_api_request_records_its_duration_in_seconds(recorder_and_reader) -> None:
    recorder, reader, dispatcher = recorder_and_reader
    send(dispatcher, "api_request", **_api_request())
    assert dispatcher.flush(FLUSH)
    point = _points(reader)["gen_ai.client.operation.duration"][0]
    assert point.count == 1
    assert 0.86 < point.sum < 0.88
    assert point.attributes["gen_ai.provider.name"] == "anthropic"
    assert point.attributes["gen_ai.request.model"] == "claude-opus-5"


def test_duration_comes_from_the_timestamps_not_the_reported_field(recorder_and_reader) -> None:
    """Hermes documents api_duration in milliseconds and reports it in seconds."""
    recorder, reader, dispatcher = recorder_and_reader
    now = time.time()
    send(
        dispatcher,
        "api_request",
        **_api_request(started_at=now, ended_at=now + 2.0, api_duration=999),
    )
    assert dispatcher.flush(FLUSH)
    point = _points(reader)["gen_ai.client.operation.duration"][0]
    assert 1.99 < point.sum < 2.01


def test_every_token_bucket_is_counted_separately(recorder_and_reader) -> None:
    recorder, reader, dispatcher = recorder_and_reader
    send(dispatcher, "api_request", **_api_request())
    assert dispatcher.flush(FLUSH)
    by_type = {
        point.attributes["gen_ai.token.type"]: point.value
        for point in _points(reader)["gen_ai.client.token.usage"]
    }
    assert by_type == {
        "input": 100,
        "output": 20,
        "cache_read": 7,
        "cache_write": 3,
        "reasoning": 5,
    }


def test_a_successful_request_counts_as_ok(recorder_and_reader) -> None:
    recorder, reader, dispatcher = recorder_and_reader
    send(dispatcher, "api_request", **_api_request())
    assert dispatcher.flush(FLUSH)
    point = _points(reader)["hermes.gen_ai.requests"][0]
    assert point.value == 1
    assert point.attributes["hermes.outcome"] == "ok"
    assert point.attributes["gen_ai.response.finish_reasons"] == "stop"


def test_a_provider_error_counts_by_reason_and_status(recorder_and_reader) -> None:
    recorder, reader, dispatcher = recorder_and_reader
    now = time.time()
    send(
        dispatcher,
        "api_error",
        session_id=SESSION,
        turn_id=TURN,
        api_request_id=REQUEST,
        platform="cli",
        model="anthropic/claude-opus-5",
        provider="anthropic",
        started_at=now,
        ended_at=now + 0.05,
        api_duration=0.05,
        status_code=429,
        retry_count=1,
        retryable=True,
        reason="rate_limited",
        error="429 Too Many Requests",
    )
    assert dispatcher.flush(FLUSH)
    point = _points(reader)["hermes.gen_ai.requests"][0]
    assert point.attributes["hermes.outcome"] == "error"
    assert point.attributes["error.type"] == "rate_limited"
    assert point.attributes["http.response.status_code"] == 429


def test_a_tool_call_records_duration_and_a_count(recorder_and_reader) -> None:
    recorder, reader, dispatcher = recorder_and_reader
    send(
        dispatcher,
        "tool_call",
        tool_name="shell_exec",
        args={"command": "ls"},
        result="ok",
        session_id=SESSION,
        turn_id=TURN,
        tool_call_id=CALL,
        duration_ms=143,
        status="ok",
        error_type=None,
        error_message=None,
        platform="cli",
    )
    assert dispatcher.flush(FLUSH)
    points = _points(reader)
    duration = points["hermes.tool.duration"][0]
    assert 0.142 < duration.sum < 0.144
    assert duration.attributes["gen_ai.tool.name"] == "shell_exec"
    calls = points["hermes.tool.calls"][0]
    assert calls.value == 1
    assert calls.attributes["hermes.outcome"] == "ok"


def test_a_failed_tool_call_carries_its_error_type(recorder_and_reader) -> None:
    recorder, reader, dispatcher = recorder_and_reader
    send(
        dispatcher,
        "tool_call",
        tool_name="shell_exec",
        args={},
        result="",
        session_id=SESSION,
        turn_id=TURN,
        tool_call_id=CALL,
        duration_ms=5,
        status="error",
        error_type="TimeoutError",
        error_message="slow",
        platform="cli",
    )
    assert dispatcher.flush(FLUSH)
    calls = _points(reader)["hermes.tool.calls"][0]
    assert calls.attributes["hermes.outcome"] == "error"
    assert calls.attributes["error.type"] == "TimeoutError"


def test_sessions_are_counted_by_outcome(recorder_and_reader) -> None:
    recorder, reader, dispatcher = recorder_and_reader
    send(
        dispatcher,
        "session_end",
        session_id=SESSION,
        completed=True,
        interrupted=False,
        platform="cli",
    )
    send(
        dispatcher,
        "session_end",
        session_id=SESSION,
        completed=False,
        interrupted=True,
        platform="cli",
    )
    assert dispatcher.flush(FLUSH)
    by_outcome = {
        point.attributes["hermes.outcome"]: point.value
        for point in _points(reader)["hermes.sessions"]
    }
    assert by_outcome == {"completed": 1, "interrupted": 1}


def test_a_request_without_usage_still_records_a_duration(recorder_and_reader) -> None:
    recorder, reader, dispatcher = recorder_and_reader
    send(dispatcher, "api_request", **_api_request(usage=None))
    assert dispatcher.flush(FLUSH)
    points = _points(reader)
    assert points["gen_ai.client.operation.duration"][0].count == 1
    assert "gen_ai.client.token.usage" not in points


# One series per attribute set lives for the process, so identifiers stay off.
def test_no_identifier_ever_reaches_a_metric_attribute(recorder_and_reader) -> None:
    recorder, reader, dispatcher = recorder_and_reader
    send(dispatcher, "api_request", **_api_request())
    send(
        dispatcher,
        "tool_call",
        tool_name="shell_exec",
        args={},
        result="",
        session_id=SESSION,
        turn_id=TURN,
        tool_call_id=CALL,
        duration_ms=1,
        status="ok",
        error_type=None,
        error_message=None,
        platform="cli",
    )
    send(
        dispatcher,
        "session_end",
        session_id=SESSION,
        completed=True,
        interrupted=False,
        platform="cli",
    )
    assert dispatcher.flush(FLUSH)
    seen = {
        str(value)
        for points in _points(reader).values()
        for point in points
        for value in point.attributes.values()
    }
    assert not seen & {SESSION, TURN, REQUEST, CALL}
    assert not any("canary" in value for value in seen)


def test_a_foreign_queue_item_is_ignored(recorder_and_reader) -> None:
    recorder, reader, dispatcher = recorder_and_reader
    assert dispatcher.submit("not an event")
    assert dispatcher.flush(FLUSH)
    assert dispatcher.stats().failed == 0


def test_a_request_with_no_model_at_all_is_still_counted(recorder_and_reader) -> None:
    recorder, reader, dispatcher = recorder_and_reader
    send(dispatcher, "api_request", **_api_request(model=None, response_model=None))
    assert dispatcher.flush(FLUSH)
    point = _points(reader)["gen_ai.client.operation.duration"][0]
    assert point.attributes["gen_ai.request.model"] == "unknown"


def test_the_reported_duration_is_used_when_timestamps_are_absent(recorder_and_reader) -> None:
    recorder, reader, dispatcher = recorder_and_reader
    send(
        dispatcher, "api_request", **_api_request(started_at=None, ended_at=None, api_duration=0.5)
    )
    assert dispatcher.flush(FLUSH)
    assert 0.49 < _points(reader)["gen_ai.client.operation.duration"][0].sum < 0.51


def test_a_request_with_neither_timestamps_nor_duration_records_zero(recorder_and_reader) -> None:
    recorder, reader, dispatcher = recorder_and_reader
    send(
        dispatcher, "api_request", **_api_request(started_at=None, ended_at=None, api_duration=None)
    )
    assert dispatcher.flush(FLUSH)
    assert _points(reader)["gen_ai.client.operation.duration"][0].sum == 0.0


def test_the_billing_route_is_a_dimension(recorder_and_reader) -> None:
    recorder, reader, dispatcher = recorder_and_reader
    send(dispatcher, "api_request", **_api_request())
    assert dispatcher.flush(FLUSH)
    point = _points(reader)["gen_ai.client.operation.duration"][0]
    assert point.attributes["hermes.billing_mode"] == "official_docs_snapshot"


def test_an_event_kind_this_recorder_does_not_consume_is_ignored(recorder_and_reader) -> None:
    recorder, reader, dispatcher = recorder_and_reader
    assert dispatcher.submit(stamp("session_start", {"session_id": SESSION}))
    assert dispatcher.flush(FLUSH)
    assert dispatcher.stats().failed == 0
