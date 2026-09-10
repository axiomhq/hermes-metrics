"""Log records built from Hermes observer hooks.

The body and attributes stay content-free at the default level: an error class,
a status code and the identifiers needed to find the matching span. Raw provider
and tool error text is content, and appears only at the levels that permit it.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from opentelemetry._logs import SeverityNumber
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)

from hermess_metrics.dispatch import Dispatcher
from hermess_metrics.events import stamp
from hermess_metrics.logs import LogRecorder
from hermess_metrics.redaction import LEVEL_FULL, LEVEL_METADATA, Redactor

FLUSH = 5.0
SESSION = "sess-1"
TURN = "turn-1"
LEAKY_ERROR = "429 for prompt 'my password is hunter2'"


@pytest.fixture
def recorder_and_logs(request: pytest.FixtureRequest):
    level = getattr(request, "param", LEVEL_METADATA)
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    dispatcher: Dispatcher[object] = Dispatcher(capacity=128)
    recorder = LogRecorder(
        logger=provider.get_logger("hermess-metrics"),
        redactor=Redactor.for_level(level),
        dispatcher=dispatcher,
    )
    dispatcher.start(recorder.handle)
    yield recorder, exporter, dispatcher
    dispatcher.stop(FLUSH)


def _records(dispatcher: Dispatcher[object], exporter: InMemoryLogRecordExporter) -> list[Any]:
    assert dispatcher.flush(FLUSH)
    return [item.log_record for item in exporter.get_finished_logs()]


def _error_payload(**overrides: Any) -> dict[str, Any]:
    now = time.time()
    payload: dict[str, Any] = {
        "session_id": SESSION,
        "turn_id": TURN,
        "api_request_id": "req-1",
        "platform": "cli",
        "model": "anthropic/claude-opus-5",
        "provider": "anthropic",
        "started_at": now,
        "ended_at": now + 0.05,
        "status_code": 429,
        "retry_count": 1,
        "retryable": True,
        "reason": "rate_limited",
        "error": LEAKY_ERROR,
    }
    payload.update(overrides)
    return payload


def test_a_provider_error_becomes_one_record(recorder_and_logs) -> None:
    recorder, exporter, dispatcher = recorder_and_logs
    recorder.api_request_error(**_error_payload())
    records = _records(dispatcher, exporter)
    assert len(records) == 1
    assert records[0].attributes["error.type"] == "rate_limited"
    assert records[0].attributes["http.response.status_code"] == 429


def test_identifiers_are_carried_so_the_span_can_be_found(recorder_and_logs) -> None:
    recorder, exporter, dispatcher = recorder_and_logs
    recorder.api_request_error(**_error_payload())
    attributes = _records(dispatcher, exporter)[0].attributes
    assert attributes["hermes.session_id"] == SESSION
    assert attributes["hermes.turn_id"] == TURN
    assert attributes["hermes.api_request_id"] == "req-1"


def test_a_retryable_failure_is_a_warning_and_a_final_one_an_error(recorder_and_logs) -> None:
    recorder, exporter, dispatcher = recorder_and_logs
    recorder.api_request_error(**_error_payload(retryable=True))
    recorder.api_request_error(**_error_payload(retryable=False))
    severities = [record.severity_number for record in _records(dispatcher, exporter)]
    assert severities == [SeverityNumber.WARN, SeverityNumber.ERROR]


def test_the_default_level_keeps_provider_error_text_out(recorder_and_logs) -> None:
    recorder, exporter, dispatcher = recorder_and_logs
    recorder.api_request_error(**_error_payload())
    record = _records(dispatcher, exporter)[0]
    assert "hunter2" not in str(record.body)
    assert "hunter2" not in str(dict(record.attributes))


@pytest.mark.parametrize("recorder_and_logs", [LEVEL_FULL], indirect=True)
def test_the_full_level_carries_provider_error_text(recorder_and_logs) -> None:
    recorder, exporter, dispatcher = recorder_and_logs
    recorder.api_request_error(**_error_payload())
    assert _records(dispatcher, exporter)[0].attributes["exception.message"] == LEAKY_ERROR


def test_a_successful_tool_call_writes_nothing(recorder_and_logs) -> None:
    recorder, exporter, dispatcher = recorder_and_logs
    recorder.post_tool_call(
        tool_name="shell_exec",
        status="ok",
        session_id=SESSION,
        turn_id=TURN,
        tool_call_id="c1",
        duration_ms=1,
        error_type=None,
        error_message=None,
    )
    assert _records(dispatcher, exporter) == []


def test_a_failed_tool_call_is_recorded_as_an_error(recorder_and_logs) -> None:
    recorder, exporter, dispatcher = recorder_and_logs
    recorder.post_tool_call(
        tool_name="shell_exec",
        status="error",
        session_id=SESSION,
        turn_id=TURN,
        tool_call_id="c1",
        duration_ms=1,
        error_type="TimeoutError",
        error_message="slow",
    )
    record = _records(dispatcher, exporter)[0]
    assert record.severity_number is SeverityNumber.ERROR
    assert record.attributes["gen_ai.tool.name"] == "shell_exec"
    assert record.attributes["error.type"] == "TimeoutError"


def test_a_plugin_diagnostic_is_recorded(recorder_and_logs) -> None:
    recorder, exporter, dispatcher = recorder_and_logs
    recorder.diagnostic("queue overflowed", dropped=17)
    record = _records(dispatcher, exporter)[0]
    assert record.body == "queue overflowed"
    assert record.attributes["dropped"] == 17
    assert record.severity_number is SeverityNumber.WARN


def test_a_foreign_queue_item_is_ignored(recorder_and_logs) -> None:
    recorder, exporter, dispatcher = recorder_and_logs
    assert dispatcher.submit("not an event")
    assert _records(dispatcher, exporter) == []
    assert dispatcher.stats().failed == 0


def test_the_record_timestamp_is_when_the_hook_fired(recorder_and_logs) -> None:
    recorder, exporter, dispatcher = recorder_and_logs
    fired_at = time.time()
    recorder.api_request_error(**_error_payload())
    record = _records(dispatcher, exporter)[0]
    assert abs(record.timestamp / 1_000_000_000 - fired_at) < 0.5


@pytest.mark.parametrize("recorder_and_logs", [LEVEL_FULL], indirect=True)
def test_the_full_level_carries_the_tool_error_message(recorder_and_logs) -> None:
    recorder, exporter, dispatcher = recorder_and_logs
    recorder.post_tool_call(
        tool_name="shell_exec",
        status="error",
        session_id=SESSION,
        turn_id=TURN,
        tool_call_id="c1",
        duration_ms=1,
        error_type="TimeoutError",
        error_message="killed after 30s running: cat /etc/shadow",
    )
    record = _records(dispatcher, exporter)[0]
    assert "cat /etc/shadow" in record.attributes["exception.message"]


def test_an_event_kind_this_recorder_does_not_consume_is_ignored(recorder_and_logs) -> None:
    recorder, exporter, dispatcher = recorder_and_logs
    assert dispatcher.submit(stamp("api_request", {"session_id": SESSION}))
    assert _records(dispatcher, exporter) == []
    assert dispatcher.stats().failed == 0
