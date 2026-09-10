# SPDX-License-Identifier: Apache-2.0 OR MIT
"""Counts approval requests and the decisions returned."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opentelemetry.metrics import Meter

from .events import KIND_APPROVAL_REQUEST, KIND_APPROVAL_RESPONSE, Event

MAX_PATTERNS = 128
OTHER_PATTERN = "other"
UNKNOWN = "unknown"


def _text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) and value else UNKNOWN


class ApprovalRecorder:
    """Counts approvals asked for and how they were answered."""

    def __init__(self, meter: Meter, max_patterns: int = MAX_PATTERNS) -> None:
        self._max_patterns = max(1, max_patterns)
        self._patterns: set[str] = set()
        self._requested = meter.create_counter(
            "hermes.approvals.requested",
            unit="{approval}",
            description="Approvals Hermes asked the operator for",
        )
        self._resolved = meter.create_counter(
            "hermes.approvals.resolved",
            unit="{approval}",
            description="Approvals answered, by decision",
        )

    @property
    def max_patterns(self) -> int:
        return self._max_patterns

    def handle(self, event: Any) -> None:
        if not isinstance(event, Event):
            return
        if event.kind == KIND_APPROVAL_REQUEST:
            self._requested.add(1, self._dimensions(event.payload))
        elif event.kind == KIND_APPROVAL_RESPONSE:
            self._resolved.add(
                1,
                {
                    **self._dimensions(event.payload),
                    "hermes.decision": _text(event.payload, "choice"),
                },
            )

    def _dimensions(self, payload: Mapping[str, Any]) -> dict[str, str]:
        return {
            "hermes.approval_pattern": self._pattern(_text(payload, "pattern_key")),
            "hermes.surface": _text(payload, "surface"),
        }

    def _pattern(self, key: str) -> str:
        if key in self._patterns:
            return key
        if len(self._patterns) >= self._max_patterns:
            return OTHER_PATTERN
        self._patterns.add(key)
        return key
