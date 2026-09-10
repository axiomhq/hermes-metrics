"""Publishes the registered tool list and per-tool call counts."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from opentelemetry.metrics import CallbackOptions, Meter, Observation

from .events import KIND_TOOL_CALL, Event

MAX_TRACKED = 512
UNKNOWN_TOOLSET = "unknown"

logger = logging.getLogger(__name__)


def installed_tools() -> dict[str, str]:
    """Tool name to toolset, as Hermes currently has them registered."""
    try:
        from tools.registry import registry

        names = registry.get_all_tool_names()
    except Exception:
        return {}
    found: dict[str, str] = {}
    for name in names:
        try:
            entry = registry.get_entry(name)
        except Exception:
            entry = None
        found[name] = str(getattr(entry, "toolset", "") or UNKNOWN_TOOLSET)
    return found


class ToolInventory:
    """Publishes the tool list and a call count seeded from it."""

    def __init__(
        self,
        meter: Meter,
        source: Callable[[], Mapping[str, str]] = installed_tools,
        max_tracked: int = MAX_TRACKED,
    ) -> None:
        self._source = source
        self._max_tracked = max(1, max_tracked)
        self._counts: dict[str, int] = {}
        meter.create_observable_gauge(
            "hermes.tools.installed",
            callbacks=[self._installed_callback],
            unit="{tool}",
            description="Tools Hermes has registered",
        )
        meter.create_observable_counter(
            "hermes.tool.invocations",
            callbacks=[self._invocations_callback],
            unit="{call}",
            description="Tool executions, zero for an installed tool never called",
        )

    def handle(self, event: Any) -> None:
        if not isinstance(event, Event) or event.kind != KIND_TOOL_CALL:
            return
        name = str(event.payload.get("tool_name") or "")
        if not name:
            return
        self._counts[name] = self._counts.get(name, 0) + 1

    def _registry(self) -> dict[str, str]:
        try:
            return dict(self._source())
        except Exception:
            logger.debug("tool registry unavailable", exc_info=True)
            return {}

    def _tracked(self) -> dict[str, str]:
        merged = self._registry()
        for name in self._counts:
            merged.setdefault(name, UNKNOWN_TOOLSET)
        return dict(sorted(merged.items())[: self._max_tracked])

    def _observe(self, value_of: Callable[[str], int]) -> Iterable[Observation]:
        return [
            Observation(
                value_of(name),
                {"gen_ai.tool.name": name, "hermes.toolset": toolset},
            )
            for name, toolset in self._tracked().items()
        ]

    def _installed_callback(self, options: CallbackOptions) -> Iterable[Observation]:
        return self._observe(lambda _: 1)

    def _invocations_callback(self, options: CallbackOptions) -> Iterable[Observation]:
        return self._observe(lambda name: self._counts.get(name, 0))
