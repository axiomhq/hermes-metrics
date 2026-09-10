"""Published rates for the models actually in use.

Exporting the rate alongside the token counters lets a dashboard compute spend
without a rate table of its own, and recompute history when a rate changes.

Only routes Hermes prices from its shipped table are published. Pricing an
OpenRouter or custom endpoint means fetching model metadata over the network,
and the export path is the wrong place to wait on an HTTP call.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from opentelemetry.metrics import CallbackOptions, Meter, Observation

from .billing import billing_mode
from .events import KIND_API_ERROR, KIND_API_REQUEST, Event

MAX_TRACKED_MODELS = 64
PRICED_ROUTES = frozenset({"official_docs_snapshot", "subscription_included"})

# Rates are per million tokens, the unit every published price list quotes.
_RATE_FIELDS = (
    ("input_token_cost", "input_cost_per_million"),
    ("output_token_cost", "output_cost_per_million"),
    ("cache_read_token_cost", "cache_read_cost_per_million"),
    ("cache_write_token_cost", "cache_write_cost_per_million"),
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Rates:
    """One model's published rates, in dollars per million tokens."""

    provider: str
    model: str
    version: str
    values: Mapping[str, float]


def _entry_for(model: str, provider: str) -> Any:
    from agent.usage_pricing import get_pricing_entry

    return get_pricing_entry(model, provider=provider, base_url="")


def resolve(model: str, provider: str) -> Rates | None:
    """Look up a route's rates, or ``None`` when they are not free to obtain."""
    if billing_mode(model, provider, "") not in PRICED_ROUTES:
        return None
    try:
        entry = _entry_for(model, provider)
    except Exception:
        return None
    if entry is None:
        return None
    values: dict[str, float] = {}
    for name, field in _RATE_FIELDS:
        amount = getattr(entry, field, None)
        if amount is not None:
            values[name] = float(amount)
    if not values:
        return None
    bare = model.rsplit("/", 1)[-1]
    return Rates(
        provider=provider,
        model=bare,
        version=str(getattr(entry, "pricing_version", "") or "unknown"),
        values=values,
    )


class PriceRecorder:
    """Publishes rates for every model this process has actually called."""

    def __init__(self, meter: Meter, max_tracked: int = MAX_TRACKED_MODELS) -> None:
        self._max_tracked = max(1, max_tracked)
        self._rates: dict[tuple[str, str], Rates] = {}
        self._skipped: set[tuple[str, str]] = set()
        for name, _ in _RATE_FIELDS:
            meter.create_observable_gauge(
                f"hermes.gen_ai.{name}",
                callbacks=[self._callback(name)],
                unit="USD/Mtok",
                description=f"Published {name.replace('_', ' ')} per million tokens",
            )

    @property
    def tracked(self) -> int:
        return len(self._rates)

    def handle(self, event: Any) -> None:
        if not isinstance(event, Event) or event.kind not in (KIND_API_REQUEST, KIND_API_ERROR):
            return
        payload = event.payload
        model = str(payload.get("model") or "")
        provider = str(payload.get("provider") or "")
        if not model:
            return
        key = (provider, model)
        if key in self._rates or key in self._skipped:
            return
        if len(self._rates) >= self._max_tracked:
            return
        rates = resolve(model, provider)
        if rates is None:
            self._skipped.add(key)
            return
        self._rates[key] = rates

    def _callback(self, name: str) -> Any:
        def observe(options: CallbackOptions) -> Iterable[Observation]:
            return [
                Observation(
                    rates.values[name],
                    {
                        "gen_ai.provider.name": rates.provider,
                        "gen_ai.request.model": rates.model,
                        "hermes.pricing_version": rates.version,
                    },
                )
                for rates in self._rates.values()
                if name in rates.values
            ]

        observe.__name__ = f"hermess_metrics_{name}"
        return observe
