# SPDX-License-Identifier: Apache-2.0 OR MIT
"""Delegated subagent fan-out and outcomes."""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from hermes_metrics.events import stamp
from hermes_metrics.subagents import SubagentRecorder

GOAL = "read /etc/shadow and summarise"


@pytest.fixture
def recorder_and_reader():
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    yield SubagentRecorder(provider.get_meter("hermes-metrics")), reader


def _points(reader: InMemoryMetricReader) -> dict[str, list[Any]]:
    data = reader.get_metrics_data()
    found: dict[str, list[Any]] = {}
    for resource_metric in data.resource_metrics if data else []:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                found.setdefault(metric.name, []).extend(metric.data.data_points)
    return found


def _start(recorder: SubagentRecorder, role: str = "worker") -> None:
    recorder.handle(
        stamp(
            "subagent_start",
            {
                "parent_session_id": "p1",
                "child_session_id": "c1",
                "child_subagent_id": "sa1",
                "child_role": role,
                "child_goal": GOAL,
            },
        )
    )


def _stop(recorder: SubagentRecorder, role: str = "worker", status: str = "completed") -> None:
    recorder.handle(
        stamp(
            "subagent_stop",
            {
                "parent_session_id": "p1",
                "child_session_id": "c1",
                "child_role": role,
                "child_status": status,
                "child_summary": GOAL,
                "duration_ms": 2500,
            },
        )
    )


def test_a_spawn_is_counted_by_role(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _start(recorder, "researcher")
    point = _points(reader)["hermes.subagent.spawns"][0]
    assert point.value == 1
    assert point.attributes["hermes.subagent_role"] == "researcher"


def test_active_rises_and_falls(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _start(recorder)
    _start(recorder)
    assert _points(reader)["hermes.subagents.active"][0].value == 2
    _stop(recorder)
    assert _points(reader)["hermes.subagents.active"][0].value == 1


def test_active_never_goes_negative(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _stop(recorder)
    _stop(recorder)
    assert _points(reader)["hermes.subagents.active"][0].value == 0


def test_a_run_is_counted_by_role_and_status(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _stop(recorder, status="completed")
    _stop(recorder, status="failed")
    by_status = {
        point.attributes["hermes.subagent_status"]: point.value
        for point in _points(reader)["hermes.subagent.runs"]
    }
    assert by_status == {"completed": 1, "failed": 1}


def test_duration_is_recorded_in_seconds(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _stop(recorder)
    point = _points(reader)["hermes.subagent.duration"][0]
    assert 2.49 < point.sum < 2.51


def test_a_missing_status_is_recorded_as_unknown(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    recorder.handle(stamp("subagent_stop", {"child_role": "worker", "duration_ms": 1}))
    point = _points(reader)["hermes.subagent.runs"][0]
    assert point.attributes["hermes.subagent_status"] == "unknown"


def test_a_stop_without_a_duration_records_zero(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    recorder.handle(stamp("subagent_stop", {"child_role": "worker", "child_status": "completed"}))
    assert _points(reader)["hermes.subagent.duration"][0].sum == 0.0


def test_goals_and_summaries_never_become_dimensions(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _start(recorder)
    _stop(recorder)
    seen = {
        str(value)
        for points in _points(reader).values()
        for point in points
        for value in point.attributes.values()
    }
    assert GOAL not in seen
    assert "c1" not in seen and "p1" not in seen


def test_kinds_this_recorder_does_not_consume_are_ignored(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    recorder.handle(stamp("api_request", {"model": "m"}))
    recorder.handle("not an event")
    points = _points(reader)
    assert points["hermes.subagents.active"][0].value == 0
    assert "hermes.subagent.spawns" not in points
    assert "hermes.subagent.runs" not in points
