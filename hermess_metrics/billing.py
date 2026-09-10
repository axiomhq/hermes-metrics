"""How a provider call is billed.

Token counts cannot distinguish a subscription-included call from a metered
one, which makes this the one costing fact Axiom cannot derive from the rest of
the telemetry. Hermes resolves it by string matching alone, so the label is
cheap enough to attach to every call.

Money arithmetic stays out. Hermes prices a call by fetching model metadata
over the network, which does not belong on the export path, and rates change
after the fact; multiplying the exported token counters by a rate table in a
dashboard recomputes history correctly and edits without a redeploy.
"""

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
