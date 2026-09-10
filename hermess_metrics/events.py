"""The stamped unit passed from a hook callback to the worker."""

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


# Recorders share one queue and consume only the kinds they know.
KIND_SESSION_START = "session_start"
KIND_SESSION_END = "session_end"
KIND_TURN_START = "turn_start"
KIND_TURN_END = "turn_end"
KIND_API_REQUEST = "api_request"
KIND_API_ERROR = "api_error"
KIND_TOOL_CALL = "tool_call"
KIND_APPROVAL_REQUEST = "approval_request"
KIND_APPROVAL_RESPONSE = "approval_response"
KIND_DIAGNOSTIC = "diagnostic"
