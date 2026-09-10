"""Work done inside one turn."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from opentelemetry.metrics import Meter

from .events import (
    KIND_API_ERROR,
    KIND_API_REQUEST,
    KIND_TOOL_CALL,
    KIND_TURN_END,
    KIND_TURN_START,
    Event,
)

DEFAULT_MAX_LIVE = 256
UNKNOWN = "unknown"


@dataclass
class _Turn:
    started_at: float
    platform: str
    api_calls: int = 0
    tool_calls: int = 0


class TurnRecorder:
    """Counts the API and tool calls a turn made, and how long it took."""

    def __init__(self, meter: Meter, max_live: int = DEFAULT_MAX_LIVE) -> None:
        self._max_live = max(1, max_live)
        self._turns: OrderedDict[str, _Turn] = OrderedDict()
        self._api_calls = meter.create_histogram(
            "hermes.turn.api_calls",
            unit="{request}",
            description="Provider calls made in one turn",
        )
        self._tool_calls = meter.create_histogram(
            "hermes.turn.tool_calls",
            unit="{call}",
            description="Tool executions in one turn",
        )
        self._duration = meter.create_histogram(
            "hermes.turn.duration",
            unit="s",
            description="Elapsed time of a turn",
        )

    @property
    def live_turns(self) -> int:
        return len(self._turns)

    def handle(self, event: Any) -> None:
        if not isinstance(event, Event):
            return
        turn_id = str(event.payload.get("turn_id") or "")
        if not turn_id:
            return
        if event.kind == KIND_TURN_START:
            self._open(turn_id, event)
        elif event.kind == KIND_TURN_END:
            self._close(turn_id, event)
        elif event.kind in (KIND_API_REQUEST, KIND_API_ERROR):
            self._bump(turn_id, "api_calls")
        elif event.kind == KIND_TOOL_CALL:
            self._bump(turn_id, "tool_calls")

    def _open(self, turn_id: str, event: Event) -> None:
        self._turns[turn_id] = _Turn(event.observed_at, _platform(event.payload))
        while len(self._turns) > self._max_live:
            self._turns.popitem(last=False)

    def _bump(self, turn_id: str, field_name: str) -> None:
        turn = self._turns.get(turn_id)
        if turn is not None:
            setattr(turn, field_name, getattr(turn, field_name) + 1)

    def _close(self, turn_id: str, event: Event) -> None:
        turn = self._turns.pop(turn_id, None)
        if turn is None:
            return
        dimensions = {"hermes.platform": turn.platform}
        self._api_calls.record(turn.api_calls, dimensions)
        self._tool_calls.record(turn.tool_calls, dimensions)
        self._duration.record(max(0.0, event.observed_at - turn.started_at), dimensions)


def _platform(payload: Mapping[str, Any]) -> str:
    value = payload.get("platform")
    return value if isinstance(value, str) and value else UNKNOWN
