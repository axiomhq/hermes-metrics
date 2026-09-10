"""The installed skill list and per-skill load counts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from hermess_metrics.events import stamp
from hermess_metrics.skills import SKILL_TOOL, SkillInventory, scan_skills


def _write_skill(root: Path, folder: str, name: str | None = None) -> None:
    skill = root / folder
    skill.mkdir(parents=True)
    front = f"---\nname: {name}\ndescription: x\n---\n" if name else "---\ndescription: x\n---\n"
    (skill / "SKILL.md").write_text(front + "body\n")


@pytest.fixture
def skills_root(tmp_path: Path) -> Path:
    _write_skill(tmp_path, "axiom-sre", "axiom-sre")
    _write_skill(tmp_path, "nested/deep-skill", "deep-skill")
    _write_skill(tmp_path, "unnamed")
    return tmp_path


def _points(reader: InMemoryMetricReader) -> dict[str, dict[str, Any]]:
    data = reader.get_metrics_data()
    found: dict[str, dict[str, Any]] = {}
    for resource_metric in data.resource_metrics if data else []:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                found[metric.name] = {
                    point.attributes["hermes.skill"]: point for point in metric.data.data_points
                }
    return found


@pytest.fixture
def inventory_and_reader(skills_root: Path):
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    disabled: set[str] = set()
    inventory = SkillInventory(
        provider.get_meter("hermess-metrics"),
        source=lambda: scan_skills([skills_root], disabled),
    )
    yield inventory, reader, disabled


def _view(inventory: SkillInventory, skill: str, tool: str = SKILL_TOOL) -> None:
    inventory.handle(stamp("tool_call", {"tool_name": tool, "args": {"name": skill}}))


def test_skills_are_found_by_their_manifest(inventory_and_reader) -> None:
    _, reader, _ = inventory_and_reader
    listed = _points(reader)["hermes.skills.installed"]
    assert set(listed) == {"axiom-sre", "deep-skill", "unnamed"}


def test_a_skill_without_a_name_falls_back_to_its_folder(inventory_and_reader) -> None:
    _, reader, _ = inventory_and_reader
    assert "unnamed" in _points(reader)["hermes.skills.installed"]


def test_an_unloaded_skill_reports_zero_rather_than_nothing(inventory_and_reader) -> None:
    _, reader, _ = inventory_and_reader
    invocations = _points(reader)["hermes.skill.invocations"]
    assert set(invocations) == {"axiom-sre", "deep-skill", "unnamed"}
    assert all(point.value == 0 for point in invocations.values())


def test_loading_a_skill_counts_against_it(inventory_and_reader) -> None:
    inventory, reader, _ = inventory_and_reader
    _view(inventory, "axiom-sre")
    _view(inventory, "axiom-sre")
    invocations = _points(reader)["hermes.skill.invocations"]
    assert invocations["axiom-sre"].value == 2
    assert invocations["deep-skill"].value == 0


def test_a_disabled_skill_is_listed_and_marked(inventory_and_reader) -> None:
    _, reader, disabled = inventory_and_reader
    disabled.add("deep-skill")
    listed = _points(reader)["hermes.skills.installed"]
    assert listed["deep-skill"].attributes["hermes.enabled"] is False
    assert listed["axiom-sre"].attributes["hermes.enabled"] is True


def test_other_tools_do_not_count_as_skill_loads(inventory_and_reader) -> None:
    inventory, reader, _ = inventory_and_reader
    inventory.handle(stamp("tool_call", {"tool_name": "terminal", "args": {"name": "axiom-sre"}}))
    assert _points(reader)["hermes.skill.invocations"]["axiom-sre"].value == 0


def test_a_skill_view_without_a_name_is_ignored(inventory_and_reader) -> None:
    inventory, reader, _ = inventory_and_reader
    inventory.handle(stamp("tool_call", {"tool_name": SKILL_TOOL, "args": {}}))
    inventory.handle(stamp("tool_call", {"tool_name": SKILL_TOOL, "args": "not a mapping"}))
    assert all(p.value == 0 for p in _points(reader)["hermes.skill.invocations"].values())


def test_a_skill_loaded_but_not_on_disk_is_still_counted(inventory_and_reader) -> None:
    inventory, reader, _ = inventory_and_reader
    _view(inventory, "plugin:remote-skill")
    assert _points(reader)["hermes.skill.invocations"]["plugin:remote-skill"].value == 1


def test_an_unreadable_skills_dir_yields_nothing(tmp_path: Path) -> None:
    assert scan_skills([tmp_path / "missing"], set()) == {}


def test_kinds_this_recorder_does_not_consume_are_ignored(inventory_and_reader) -> None:
    inventory, reader, _ = inventory_and_reader
    inventory.handle(stamp("api_request", {"model": "m"}))
    inventory.handle("not an event")
    assert all(p.value == 0 for p in _points(reader)["hermes.skill.invocations"].values())


def test_the_scan_is_cached_between_exports(inventory_and_reader, skills_root: Path) -> None:
    inventory, reader, _ = inventory_and_reader
    _points(reader)
    _write_skill(skills_root, "added-later", "added-later")
    assert "added-later" not in _points(reader)["hermes.skills.installed"]
    inventory.forget()
    assert "added-later" in _points(reader)["hermes.skills.installed"]


def test_the_real_hermes_directories_are_readable() -> None:
    from hermess_metrics.skills import installed_skills

    assert isinstance(installed_skills(), dict)


def test_an_unreadable_manifest_falls_back_to_the_folder_name(tmp_path: Path) -> None:
    skill = tmp_path / "broken"
    skill.mkdir()
    (skill / "SKILL.md").write_bytes(b"\xff\xfe not: [valid: yaml")
    assert set(scan_skills([tmp_path], set())) == {"broken"}


def test_a_root_that_cannot_be_walked_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(self: Path, pattern: str) -> Any:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "rglob", explode)
    assert scan_skills([Path("/anywhere")], set()) == {}


def test_excluded_manifests_are_skipped(skills_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import agent.skill_utils as skill_utils

    monkeypatch.setattr(skill_utils, "is_excluded_skill_path", lambda path: "nested" in str(path))
    assert "deep-skill" not in scan_skills([skills_root], set())


def test_an_exclusion_check_that_raises_keeps_the_skill(
    skills_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent.skill_utils as skill_utils

    monkeypatch.setattr(skill_utils, "is_excluded_skill_path", _raise_exclusion)
    assert "axiom-sre" in scan_skills([skills_root], set())


def _raise_exclusion(path: Any) -> Any:
    raise RuntimeError("exclusion rules unavailable")


def test_missing_hermes_directories_yield_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent.skill_utils as skill_utils

    from hermess_metrics.skills import installed_skills

    monkeypatch.delattr(skill_utils, "get_all_skills_dirs")
    assert installed_skills() == {}


def test_a_scan_that_raises_leaves_an_empty_inventory() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    inventory = SkillInventory(provider.get_meter("x"), source=_raise_scan)
    _view(inventory, "still-counted")
    assert _points(reader)["hermes.skill.invocations"]["still-counted"].value == 1


def _raise_scan() -> Any:
    raise RuntimeError("skills directory vanished")


def test_a_frontmatter_parser_that_raises_falls_back_to_the_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent.skill_utils as skill_utils

    _write_skill(tmp_path, "resilient", "ignored-name")
    monkeypatch.setattr(skill_utils, "parse_frontmatter", _raise_parse)
    assert set(scan_skills([tmp_path], set())) == {"resilient"}


def _raise_parse(content: str) -> Any:
    raise ValueError("bad frontmatter")
