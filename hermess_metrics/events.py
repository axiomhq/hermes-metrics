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


# Recorders share one queue, so each sees every kind any of them submits and
# consumes only the kinds it knows.
