"""Approval friction.

Hermes fires approval hooks that its published reference omits. The payload
carries the raw command, which is content, and a pattern key naming the guard
that matched, which is not. Only the latter becomes a dimension, and even that
is capped: guards are a closed set in principle but nothing enforces it.
"""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from hermess_metrics.approvals import OTHER_PATTERN, ApprovalRecorder
from hermess_metrics.events import stamp

SECRET_COMMAND = "curl -H 'Authorization: Bearer sk-live-secret' https://x"


@pytest.fixture
def recorder_and_reader():
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    yield ApprovalRecorder(provider.get_meter("hermess-metrics")), reader


def _points(reader: InMemoryMetricReader) -> dict[str, list[Any]]:
    data = reader.get_metrics_data()
    found: dict[str, list[Any]] = {}
    for resource_metric in data.resource_metrics if data else []:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                found.setdefault(metric.name, []).extend(metric.data.data_points)
    return found


def _request(recorder: ApprovalRecorder, pattern: str = "rm", surface: str = "cli") -> None:
    recorder.handle(
        stamp(
            "approval_request",
            {"command": SECRET_COMMAND, "pattern_key": pattern, "surface": surface},
        )
    )


def _response(recorder: ApprovalRecorder, choice: str, pattern: str = "rm") -> None:
    recorder.handle(
        stamp(
            "approval_response",
            {
                "command": SECRET_COMMAND,
                "pattern_key": pattern,
                "surface": "cli",
                "choice": choice,
            },
        )
    )


def test_a_request_is_counted_by_pattern_and_surface(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _request(recorder)
    point = _points(reader)["hermes.approvals.requested"][0]
    assert point.value == 1
    assert point.attributes["hermes.approval_pattern"] == "rm"
    assert point.attributes["hermes.surface"] == "cli"


def test_decisions_are_counted_separately(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _response(recorder, "approve")
    _response(recorder, "approve")
    _response(recorder, "deny")
    _response(recorder, "timeout")
    by_decision = {
        point.attributes["hermes.decision"]: point.value
        for point in _points(reader)["hermes.approvals.resolved"]
    }
    assert by_decision == {"approve": 2, "deny": 1, "timeout": 1}


def test_a_missing_choice_is_recorded_as_unknown(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    recorder.handle(stamp("approval_response", {"pattern_key": "rm", "surface": "cli"}))
    point = _points(reader)["hermes.approvals.resolved"][0]
    assert point.attributes["hermes.decision"] == "unknown"


def test_the_command_never_becomes_a_dimension(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _request(recorder)
    _response(recorder, "approve")
    seen = {
        str(value)
        for points in _points(reader).values()
        for point in points
        for value in point.attributes.values()
    }
    assert not any("sk-live-secret" in value for value in seen)
    assert SECRET_COMMAND not in seen


def test_the_pattern_dimension_is_capped(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    for index in range(recorder.max_patterns + 10):
        _request(recorder, pattern=f"guard-{index}")
    patterns = {
        point.attributes["hermes.approval_pattern"]
        for point in _points(reader)["hermes.approvals.requested"]
    }
    assert OTHER_PATTERN in patterns
    assert len(patterns) == recorder.max_patterns + 1


def test_a_known_pattern_survives_the_cap(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _request(recorder, pattern="rm")
    for index in range(recorder.max_patterns + 10):
        _request(recorder, pattern=f"guard-{index}")
    _request(recorder, pattern="rm")
    by_pattern = {
        point.attributes["hermes.approval_pattern"]: point.value
        for point in _points(reader)["hermes.approvals.requested"]
    }
    assert by_pattern["rm"] == 2


def test_a_missing_pattern_is_recorded_as_unknown(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    recorder.handle(stamp("approval_request", {"surface": "cli"}))
    point = _points(reader)["hermes.approvals.requested"][0]
    assert point.attributes["hermes.approval_pattern"] == "unknown"


def test_kinds_this_recorder_does_not_consume_are_ignored(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    recorder.handle(stamp("api_request", {"model": "m"}))
    recorder.handle("not an event")
    assert _points(reader) == {}
