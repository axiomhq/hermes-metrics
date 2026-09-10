"""Resolves how a provider call is billed."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

UNKNOWN = "unknown"
BILLING_MODES = (
    "subscription_included",
    "official_models_api",
    "official_docs_snapshot",
    UNKNOWN,
)

_CACHE_SIZE = 256


def _resolver() -> Any:
    from agent.usage_pricing import resolve_billing_route

    return resolve_billing_route


@lru_cache(maxsize=_CACHE_SIZE)
def billing_mode(model: str, provider: str, base_url: str) -> str:
    """A bounded label for how this route bills, or ``unknown``."""
    try:
        route = _resolver()(model, provider=provider, base_url=base_url)
    except Exception:
        return UNKNOWN
    mode = getattr(route, "billing_mode", UNKNOWN)
    return mode if mode in BILLING_MODES else UNKNOWN
