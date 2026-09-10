# SPDX-License-Identifier: Apache-2.0 OR MIT
"""The dashboard shipped with the plugin."""

from __future__ import annotations

import json

import pytest

from hermess_metrics import dashboard

DATASETS = {"metrics": "m-set", "traces": "t-set", "logs": "l-set"}


def test_the_template_ships_with_the_package() -> None:
    assert dashboard.TEMPLATE.is_file()


def test_the_template_carries_no_real_dataset_names() -> None:
    """Otherwise it would chart the org it was captured from."""
    raw = dashboard.TEMPLATE.read_text()
    assert "hermes-metrics" not in raw
    assert "hermes-traces" not in raw
    assert "hermes-logs" not in raw


def test_every_placeholder_is_substituted() -> None:
    text = json.dumps(dashboard.document(DATASETS))
    assert "{{" not in text
    for name in DATASETS.values():
        assert name in text


def test_the_document_keeps_its_panels_and_layout() -> None:
    doc = dashboard.document(DATASETS)
    assert len(doc["charts"]) == len(doc["layout"]) > 20
    assert {c["id"] for c in doc["charts"]} == {p["i"] for p in doc["layout"]}


def test_every_layout_row_fills_the_grid() -> None:
    rows: dict[int, int] = {}
    for panel in dashboard.document(DATASETS)["layout"]:
        rows[panel["y"]] = rows.get(panel["y"], 0) + panel["w"]
    assert set(rows.values()) == {12}


def test_the_name_can_be_overridden() -> None:
    assert dashboard.document(DATASETS, "Mine")["name"] == "Mine"
    assert dashboard.document(DATASETS)["name"]


def test_a_missing_signal_is_reported(recwarn: pytest.WarningsRecorder) -> None:
    assert dashboard.missing_signals(DATASETS) == []
    assert dashboard.missing_signals({"metrics": "m"}) == ["traces", "logs"]
    assert dashboard.missing_signals({}) == ["metrics", "traces", "logs"]


def test_the_document_is_json_serialisable() -> None:
    json.dumps(dashboard.document(DATASETS))
