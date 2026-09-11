# SPDX-License-Identifier: Apache-2.0 OR MIT
"""The dashboard shipped with the plugin."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TEMPLATE = Path(__file__).with_name("dashboard.json")
PLACEHOLDERS = ("metrics", "traces", "logs")


def document(datasets: dict[str, str], name: str = "") -> dict[str, Any]:
    """The dashboard with this install's dataset names substituted in."""
    text = TEMPLATE.read_text()
    for signal in PLACEHOLDERS:
        text = text.replace("{{" + signal + "}}", datasets.get(signal, ""))
    dashboard: dict[str, Any] = json.loads(text)
    if name:
        dashboard["name"] = name
    return dashboard


def missing_signals(datasets: dict[str, str]) -> list[str]:
    """Signals the dashboard needs that this install is not sending."""
    return [signal for signal in PLACEHOLDERS if not datasets.get(signal)]
