# SPDX-License-Identifier: Apache-2.0 OR MIT
"""The monitor pack setup creates."""

from __future__ import annotations

from typing import Any

import pytest

from hermes_metrics import alerts
from hermes_metrics.control_plane import AxiomError

DATASETS = {"metrics": "hermes-metrics", "traces": "hermes-traces", "logs": "hermes-logs"}
EXPECTED = 7


class _Plane:
    def __init__(self, refuse: set[str] | None = None) -> None:
        self.seen: list[dict[str, Any]] = []
        self.refuse = refuse or set()

    def create_monitor(self, spec: dict[str, Any]) -> dict[str, Any]:
        self.seen.append(spec)
        if spec["name"] in self.refuse:
            raise AxiomError(400, "invalid field")
        return {"id": "m1"}


def test_the_pack_covers_the_metrics_dataset() -> None:
    assert len({spec["name"] for spec in alerts.pack(DATASETS)}) == EXPECTED


def test_every_monitor_queries_metrics_not_traces() -> None:
    """Traces would rescan raw spans to recompute what the metrics already hold."""
    for spec in alerts.pack(DATASETS):
        assert "mplQuery" in spec
        assert "aplQuery" not in spec
        assert "hermes-metrics" in spec["mplQuery"]


def test_identifiers_are_backtick_escaped() -> None:
    """MPL escapes with backticks, and every name here has a dot or a hyphen."""
    for spec in alerts.pack(DATASETS):
        assert spec["mplQuery"].startswith("`hermes-metrics`:`")


def test_without_a_metrics_dataset_there_is_nothing_to_alert_on() -> None:
    assert alerts.pack({"traces": "t", "logs": "l"}) == []


def test_the_tunable_defaults_are_stated_in_the_description() -> None:
    by_name = {spec["name"]: spec for spec in alerts.pack(DATASETS)}
    tokens = by_name["Hermes token use is high"]
    assert f"{alerts.TOKEN_BUDGET_PER_15M:,}" in tokens["description"]
    assert tokens["threshold"] == alerts.TOKEN_BUDGET_PER_15M
    spend = by_name["Hermes spend is high"]
    assert spend["threshold"] == alerts.SPEND_BUDGET_PER_HOUR


def test_the_silence_alert_asks_axiom_to_fire_on_no_data() -> None:
    silence = [s for s in alerts.pack(DATASETS) if s.get("alertOnNoData")]
    assert len(silence) == 1
    assert "stopped reporting" in silence[0]["name"]


def test_creating_the_pack_reports_each_outcome() -> None:
    plane = _Plane(refuse={"Hermes stopped reporting"})
    report = alerts.create(plane, DATASETS)
    assert report.total == EXPECTED
    assert len(report.created) == EXPECTED - 1
    assert report.failed == [("Hermes stopped reporting", "invalid field")]


def test_one_refusal_does_not_stop_the_rest() -> None:
    plane = _Plane(refuse={spec["name"] for spec in alerts.pack(DATASETS)})
    report = alerts.create(plane, DATASETS)
    assert report.created == []
    assert len(report.failed) == EXPECTED
    assert len(plane.seen) == EXPECTED


def test_every_monitor_carries_the_fields_axiom_requires() -> None:
    plane = _Plane()
    alerts.create(plane, DATASETS)
    for body in plane.seen:
        assert body["name"] and body["type"] == alerts.THRESHOLD
        assert body["operator"] == alerts.ABOVE
        assert body["intervalMinutes"] >= 1 and body["rangeMinutes"] >= 1


@pytest.mark.parametrize("field", ["intervalMinutes", "rangeMinutes", "threshold"])
def test_no_monitor_is_missing_a_required_number(field: str) -> None:
    assert all(field in spec for spec in alerts.pack(DATASETS))
