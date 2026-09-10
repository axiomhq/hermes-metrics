"""The plugin's own queue accounting, published as metrics."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from hermess_metrics.dispatch import Dispatcher
from hermess_metrics.health import HealthMetrics

FLUSH = 5.0


def _values(reader: InMemoryMetricReader) -> dict[str, float]:
    data = reader.get_metrics_data()
    found: dict[str, float] = {}
    for resource_metric in data.resource_metrics if data else []:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                for point in metric.data.data_points:
                    found[metric.name] = getattr(point, "value", 0)
    return found


@pytest.fixture
def wired():
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    dispatcher: Dispatcher[Any] = Dispatcher(capacity=4)
    HealthMetrics(provider.get_meter("hermess-metrics"), dispatcher)
    yield dispatcher, reader
    dispatcher.stop(FLUSH)


def test_an_idle_plugin_reports_zeroes(wired) -> None:
    dispatcher, reader = wired
    dispatcher.start(lambda _: None)
    values = _values(reader)
    assert values["hermes.telemetry.accepted"] == 0
    assert values["hermes.telemetry.dropped"] == 0
    assert values["hermes.telemetry.queue_depth"] == 0


def test_handled_work_is_counted(wired) -> None:
    dispatcher, reader = wired
    dispatcher.start(lambda _: None)
    for value in range(3):
        assert dispatcher.submit(value)
    assert dispatcher.flush(FLUSH)
    values = _values(reader)
    assert values["hermes.telemetry.accepted"] == 3
    assert values["hermes.telemetry.handled"] == 3
    assert values["hermes.telemetry.failed"] == 0


def test_drops_are_visible(wired) -> None:
    dispatcher, reader = wired
    release = threading.Event()
    dispatcher.start(lambda _: release.wait(FLUSH))
    try:
        while dispatcher.submit(0):
            pass
        assert _values(reader)["hermes.telemetry.dropped"] >= 1
    finally:
        release.set()


def test_a_failing_handler_is_counted_separately(wired) -> None:
    dispatcher, reader = wired
    dispatcher.start(_boom)
    assert dispatcher.submit(1)
    assert dispatcher.flush(FLUSH)
    values = _values(reader)
    assert values["hermes.telemetry.failed"] == 1
    assert values["hermes.telemetry.handled"] == 0


def _boom(item: Any) -> None:
    raise RuntimeError("nope")


def test_queue_depth_reflects_pending_work(wired) -> None:
    dispatcher, reader = wired
    release = threading.Event()
    dispatcher.start(lambda _: release.wait(FLUSH))
    try:
        while dispatcher.submit(0):
            pass
        assert _values(reader)["hermes.telemetry.queue_depth"] >= 1
    finally:
        release.set()


def test_depth_is_zero_before_the_worker_starts() -> None:
    dispatcher: Dispatcher[int] = Dispatcher(capacity=4)
    assert dispatcher.depth == 0
