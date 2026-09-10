"""The unit passed from a hook callback to the worker thread.

The stamp is taken on the agent thread at the moment the hook fires, so a
recorder can place work at the time it happened rather than the time the queue
reached it.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Event:
    kind: str
    payload: Mapping[str, Any]
    observed_at: float


def stamp(kind: str, payload: Mapping[str, Any]) -> Event:
    return Event(kind, payload, time.time())


# Recorders share one queue, so each sees every kind submitted and consumes
# only the kinds it knows. Submission happens once per hook, above them.
KIND_SESSION_START = "session_start"
KIND_SESSION_END = "session_end"
KIND_TURN_START = "turn_start"
KIND_TURN_END = "turn_end"
KIND_API_REQUEST = "api_request"
KIND_API_ERROR = "api_error"
KIND_TOOL_CALL = "tool_call"
KIND_DIAGNOSTIC = "diagnostic"
