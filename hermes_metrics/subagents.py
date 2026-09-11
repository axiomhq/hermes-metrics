# SPDX-License-Identifier: Apache-2.0 OR MIT
"""Delegated subagent fan-out and outcomes."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from opentelemetry.metrics import CallbackOptions, Meter, Observation

from .events import KIND_SUBAGENT_START, KIND_SUBAGENT_STOP, Event

UNKNOWN = "unknown"


def _text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) and value else UNKNOWN


class SubagentRecorder:
    """Counts delegated subagents, their outcomes and how many run at once."""

    def __init__(self, meter: Meter) -> None:
        self._active = 0
        self._spawns = meter.create_counter(
            "hermes.subagent.spawns",
            unit="{subagent}",
            description="Subagents delegated",
        )
        self._runs = meter.create_counter(
            "hermes.subagent.runs",
            unit="{subagent}",
            description="Subagents finished, by outcome",
        )
        self._duration = meter.create_histogram(
            "hermes.subagent.duration",
            unit="s",
            description="Elapsed time of a subagent run",
        )
        meter.create_observable_gauge(
            "hermes.subagents.active",
            callbacks=[self._active_callback],
            unit="{subagent}",
            description="Subagents currently running",
        )

    def handle(self, event: Any) -> None:
        if not isinstance(event, Event):
            return
        if event.kind == KIND_SUBAGENT_START:
            self._start(event.payload)
        elif event.kind == KIND_SUBAGENT_STOP:
            self._stop(event.payload)

    def _start(self, payload: Mapping[str, Any]) -> None:
        self._active += 1
        self._spawns.add(1, {"hermes.subagent_role": _text(payload, "child_role")})

    def _stop(self, payload: Mapping[str, Any]) -> None:
        # A stop without a matching start would otherwise drive the gauge negative.
        self._active = max(0, self._active - 1)
        role = _text(payload, "child_role")
        duration_ms = payload.get("duration_ms")
        elapsed = float(duration_ms) / 1000.0 if isinstance(duration_ms, (int, float)) else 0.0
        self._duration.record(elapsed, {"hermes.subagent_role": role})
        self._runs.add(
            1,
            {
                "hermes.subagent_role": role,
                "hermes.subagent_status": _text(payload, "child_status"),
            },
        )

    def _active_callback(self, options: CallbackOptions) -> Iterable[Observation]:
        return [Observation(self._active)]
