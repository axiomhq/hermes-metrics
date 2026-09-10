# SPDX-License-Identifier: Apache-2.0 OR MIT
"""Publishes the dispatcher's own accounting as metrics."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from opentelemetry.metrics import CallbackOptions, Meter, Observation

from .dispatch import Dispatcher


class HealthMetrics:
    """Publishes the dispatcher's accounting as observable instruments."""

    def __init__(self, meter: Meter, dispatcher: Dispatcher[Any]) -> None:
        self._dispatcher = dispatcher
        for name, description, field in (
            ("accepted", "Hook events taken onto the queue", "accepted"),
            ("dropped", "Hook events refused because the queue was full", "dropped"),
            ("handled", "Hook events processed by a recorder", "handled"),
            ("failed", "Hook events a recorder raised on", "failed"),
        ):
            meter.create_observable_counter(
                f"hermes.telemetry.{name}",
                callbacks=[self._counter_callback(field)],
                unit="{event}",
                description=description,
            )
        meter.create_observable_gauge(
            "hermes.telemetry.queue_depth",
            callbacks=[self._depth_callback],
            unit="{event}",
            description="Hook events waiting to be processed",
        )

    def _counter_callback(self, field: str) -> Any:
        def observe(options: CallbackOptions) -> Iterable[Observation]:
            return [Observation(getattr(self._dispatcher.stats(), field))]

        observe.__name__ = f"hermess_metrics_{field}"
        return observe

    def _depth_callback(self, options: CallbackOptions) -> Iterable[Observation]:
        return [Observation(self._dispatcher.depth)]
