"""The registered tool list and per-tool call counts."""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from hermess_metrics.events import stamp
from hermess_metrics.inventory import MAX_TRACKED, ToolInventory

INSTALLED = {"terminal": "shell", "read_file": "files", "skill_view": "skills"}


def _points(reader: InMemoryMetricReader) -> dict[str, dict[str, Any]]:
    data = reader.get_metrics_data()
    found: dict[str, dict[str, Any]] = {}
    for resource_metric in data.resource_metrics if data else []:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                found[metric.name] = {
                    point.attributes["gen_ai.tool.name"]: point for point in metric.data.data_points
                }
    return found


@pytest.fixture
def inventory_and_reader():
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    installed = dict(INSTALLED)
    inventory = ToolInventory(provider.get_meter("hermess-metrics"), source=lambda: installed)
    yield inventory, reader, installed


def _call(inventory: ToolInventory, tool: str) -> None:
    inventory.handle(stamp("tool_call", {"tool_name": tool, "status": "ok"}))


def test_every_installed_tool_is_listed(inventory_and_reader) -> None:
    _, reader, _ = inventory_and_reader
    listed = _points(reader)["hermes.tools.installed"]
    assert set(listed) == set(INSTALLED)
    assert all(point.value == 1 for point in listed.values())


def test_the_toolset_is_carried(inventory_and_reader) -> None:
    _, reader, _ = inventory_and_reader
    listed = _points(reader)["hermes.tools.installed"]
    assert listed["terminal"].attributes["hermes.toolset"] == "shell"


def test_an_uncalled_tool_reports_zero_rather_than_nothing(inventory_and_reader) -> None:
    _, reader, _ = inventory_and_reader
    invocations = _points(reader)["hermes.tool.invocations"]
    assert set(invocations) == set(INSTALLED)
    assert all(point.value == 0 for point in invocations.values())


def test_calls_are_counted_against_the_tool(inventory_and_reader) -> None:
    inventory, reader, _ = inventory_and_reader
    _call(inventory, "terminal")
    _call(inventory, "terminal")
    _call(inventory, "read_file")
    invocations = _points(reader)["hermes.tool.invocations"]
    assert invocations["terminal"].value == 2
    assert invocations["read_file"].value == 1
    assert invocations["skill_view"].value == 0


def test_a_tool_registered_after_load_appears(inventory_and_reader) -> None:
    _, reader, installed = inventory_and_reader
    installed["late_tool"] = "plugins"
    assert "late_tool" in _points(reader)["hermes.tools.installed"]


def test_a_call_to_a_tool_not_in_the_registry_is_still_counted(inventory_and_reader) -> None:
    inventory, reader, _ = inventory_and_reader
    _call(inventory, "ghost_tool")
    invocations = _points(reader)["hermes.tool.invocations"]
    assert invocations["ghost_tool"].value == 1
    assert invocations["ghost_tool"].attributes["hermes.toolset"] == "unknown"


def test_a_registry_that_raises_leaves_the_counts_intact(inventory_and_reader) -> None:
    inventory, reader, _ = inventory_and_reader
    _call(inventory, "terminal")
    inventory._source = _raise  # type: ignore[assignment]
    invocations = _points(reader)["hermes.tool.invocations"]
    assert invocations["terminal"].value == 1


def _raise() -> Any:
    raise RuntimeError("registry unavailable")


def test_the_tracked_set_is_bounded() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    installed = {f"tool_{index}": "bulk" for index in range(MAX_TRACKED + 20)}
    ToolInventory(provider.get_meter("x"), source=lambda: installed)
    assert len(_points(reader)["hermes.tools.installed"]) == MAX_TRACKED


def test_kinds_this_recorder_does_not_consume_are_ignored(inventory_and_reader) -> None:
    inventory, reader, _ = inventory_and_reader
    inventory.handle(stamp("api_request", {"model": "m"}))
    inventory.handle("not an event")
    assert all(point.value == 0 for point in _points(reader)["hermes.tool.invocations"].values())


def test_the_real_registry_is_readable() -> None:
    """Tools appear only once their modules are imported, which is the point."""
    import tools.file_tools  # noqa: F401
    import tools.skills_tool  # noqa: F401

    from hermess_metrics.inventory import installed_tools

    found = installed_tools()
    assert "skill_view" in found
    assert found["skill_view"] == "skills"


def test_an_unreadable_registry_yields_an_empty_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    import tools.registry as registry_module

    from hermess_metrics.inventory import installed_tools

    monkeypatch.delattr(registry_module, "registry")
    assert installed_tools() == {}


def test_a_tool_whose_entry_cannot_be_read_still_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    import tools.registry as registry_module

    from hermess_metrics.inventory import UNKNOWN_TOOLSET, installed_tools

    monkeypatch.setattr(registry_module.registry, "get_all_tool_names", lambda: ["mystery"])
    monkeypatch.setattr(registry_module.registry, "get_entry", _raise_entry)
    assert installed_tools() == {"mystery": UNKNOWN_TOOLSET}


def _raise_entry(name: str) -> Any:
    raise RuntimeError("entry lookup failed")
