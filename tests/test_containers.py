# SPDX-License-Identifier: Apache-2.0 OR MIT
"""Host stats for the containers Hermes runs commands in."""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from hermes_metrics.containers import ContainerStats, parse_bytes, parse_percent

PS_OUTPUT = "abc123\ndef456\n"
STATS_OUTPUT = (
    "abc123\thermes-shell-1\t12.50%\t1.5GiB / 5GiB\t30.00%\t42\n"
    "def456\thermes-shell-2\t0.00%\t256MiB / 5GiB\t5.00%\t7\n"
)


def _runner(script: dict[str, str]):
    def run(command: list[str]) -> str:
        for key, output in script.items():
            if key in command:
                return output
        raise AssertionError(f"unexpected command {command}")

    return run


@pytest.fixture
def stats_and_reader():
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    runner = _runner({"ps": PS_OUTPUT, "stats": STATS_OUTPUT})
    yield ContainerStats(provider.get_meter("hermes-metrics"), runner=runner), reader


def _points(reader: InMemoryMetricReader) -> dict[str, dict[str, Any]]:
    data = reader.get_metrics_data()
    found: dict[str, dict[str, Any]] = {}
    for resource_metric in data.resource_metrics if data else []:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                found[metric.name] = {
                    point.attributes["hermes.container"]: point
                    for point in metric.data.data_points
                    if "hermes.container" in point.attributes
                }
    return found


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1.5GiB", 1610612736), ("256MiB", 268435456), ("1kB", 1000), ("512B", 512)],
)
def test_sizes_are_parsed(text: str, expected: int) -> None:
    assert parse_bytes(text) == expected


@pytest.mark.parametrize(("text", "expected"), [("12.50%", 12.5), ("0.00%", 0.0), ("x", None)])
def test_percentages_are_parsed(text: str, expected: float | None) -> None:
    assert parse_percent(text) == expected


def test_every_container_reports_cpu_and_memory(stats_and_reader) -> None:
    _, reader = stats_and_reader
    points = _points(reader)
    assert points["hermes.container.cpu_percent"]["hermes-shell-1"].value == 12.5
    assert points["hermes.container.memory_bytes"]["hermes-shell-1"].value == 1610612736
    assert points["hermes.container.memory_percent"]["hermes-shell-1"].value == 30.0
    assert points["hermes.container.pids"]["hermes-shell-2"].value == 7


def test_the_container_count_is_reported(stats_and_reader) -> None:
    _, reader = stats_and_reader
    data = reader.get_metrics_data()
    names = {
        metric.name
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
    }
    assert "hermes.containers.running" in names


def test_no_containers_reports_zero_running() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    ContainerStats(provider.get_meter("x"), runner=_runner({"ps": "\n"}))
    data = reader.get_metrics_data()
    running = [
        point
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
        if metric.name == "hermes.containers.running"
        for point in metric.data.data_points
    ]
    assert running[0].value == 0


def test_a_missing_docker_binary_is_silent() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    ContainerStats(provider.get_meter("x"), runner=_explode)
    data = reader.get_metrics_data()
    running = [
        point
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
        if metric.name == "hermes.containers.running"
        for point in metric.data.data_points
    ]
    assert running[0].value == 0


def _explode(command: list[str]) -> str:
    raise FileNotFoundError("docker not on PATH")


def test_a_malformed_stats_line_is_skipped() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    ContainerStats(
        provider.get_meter("x"),
        runner=_runner({"ps": "abc\n", "stats": "abc\tonly-two-fields\n"}),
    )
    assert _points(reader).get("hermes.container.cpu_percent", {}) == {}


def test_the_scan_is_cached_between_exports(stats_and_reader) -> None:
    stats, reader = stats_and_reader
    _points(reader)
    stats._runner = _explode  # type: ignore[assignment]
    assert _points(reader)["hermes.container.cpu_percent"]["hermes-shell-1"].value == 12.5
    stats.forget()
    assert _points(reader).get("hermes.container.cpu_percent", {}) == {}


@pytest.mark.parametrize("text", ["nonsense", "5 furlongs", ""])
def test_an_unparseable_size_is_none(text: str) -> None:
    assert parse_bytes(text) is None


def test_the_real_runner_returns_command_output() -> None:
    from hermes_metrics.containers import run_docker

    assert run_docker(["echo", "hello"]).strip() == "hello"
