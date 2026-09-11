# SPDX-License-Identifier: Apache-2.0 OR MIT
"""Work done inside one turn."""

from __future__ import annotations

import time
from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from hermes_metrics.events import Event, stamp
from hermes_metrics.turns import TurnRecorder

TURN = "turn-1"


@pytest.fixture
def recorder_and_reader():
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    yield TurnRecorder(provider.get_meter("hermes-metrics")), reader


def _points(reader: InMemoryMetricReader) -> dict[str, list[Any]]:
    data = reader.get_metrics_data()
    found: dict[str, list[Any]] = {}
    for resource_metric in data.resource_metrics if data else []:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                found.setdefault(metric.name, []).extend(metric.data.data_points)
    return found


def _send(recorder: TurnRecorder, kind: str, at: float | None = None, **payload: Any) -> None:
    event = stamp(kind, payload)
    recorder.handle(Event(kind, payload, at) if at is not None else event)


def test_a_turn_records_the_work_it_contained(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _send(recorder, "turn_start", turn_id=TURN, platform="cli")
    _send(recorder, "api_request", turn_id=TURN)
    _send(recorder, "api_request", turn_id=TURN)
    _send(recorder, "tool_call", turn_id=TURN)
    _send(recorder, "turn_end", turn_id=TURN, platform="cli")
    points = _points(reader)
    assert points["hermes.turn.api_calls"][0].sum == 2
    assert points["hermes.turn.tool_calls"][0].sum == 1
    assert points["hermes.turn.api_calls"][0].attributes["hermes.platform"] == "cli"


def test_a_failed_api_call_counts_toward_the_turn(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _send(recorder, "turn_start", turn_id=TURN, platform="cli")
    _send(recorder, "api_error", turn_id=TURN)
    _send(recorder, "turn_end", turn_id=TURN, platform="cli")
    assert _points(reader)["hermes.turn.api_calls"][0].sum == 1


def test_turn_duration_spans_the_hooks(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    now = time.time()
    _send(recorder, "turn_start", at=now, turn_id=TURN, platform="cli")
    _send(recorder, "turn_end", at=now + 1.5, turn_id=TURN, platform="cli")
    assert 1.49 < _points(reader)["hermes.turn.duration"][0].sum < 1.51


def test_a_turn_with_no_work_records_zeroes(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _send(recorder, "turn_start", turn_id=TURN, platform="cli")
    _send(recorder, "turn_end", turn_id=TURN, platform="cli")
    points = _points(reader)
    assert points["hermes.turn.api_calls"][0].sum == 0
    assert points["hermes.turn.tool_calls"][0].sum == 0


def test_work_outside_a_known_turn_is_ignored(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _send(recorder, "api_request", turn_id="never-started")
    _send(recorder, "turn_end", turn_id="never-started", platform="cli")
    assert _points(reader) == {}


def test_turns_are_tracked_independently(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _send(recorder, "turn_start", turn_id="a", platform="cli")
    _send(recorder, "turn_start", turn_id="b", platform="cli")
    _send(recorder, "api_request", turn_id="a")
    _send(recorder, "turn_end", turn_id="b", platform="cli")
    _send(recorder, "turn_end", turn_id="a", platform="cli")
    sums = sorted(point.sum for point in _points(reader)["hermes.turn.api_calls"])
    assert sums == [1]
    assert _points(reader)["hermes.turn.api_calls"][0].count == 2


def test_live_turn_state_is_bounded() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    recorder = TurnRecorder(provider.get_meter("x"), max_live=4)
    for index in range(20):
        _send(recorder, "turn_start", turn_id=f"t-{index}", platform="cli")
    assert recorder.live_turns == 4


def test_the_turn_id_never_becomes_a_dimension(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _send(recorder, "turn_start", turn_id=TURN, platform="cli")
    _send(recorder, "turn_end", turn_id=TURN, platform="cli")
    seen = {
        str(value)
        for points in _points(reader).values()
        for point in points
        for value in point.attributes.values()
    }
    assert TURN not in seen


def test_kinds_this_recorder_does_not_consume_are_ignored(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    recorder.handle(stamp("session_start", {"session_id": "s"}))
    recorder.handle("not an event")
    assert _points(reader) == {}
