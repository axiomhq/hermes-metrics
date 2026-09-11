# SPDX-License-Identifier: Apache-2.0 OR MIT
"""Publishes token rates for the models in use."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from opentelemetry.metrics import CallbackOptions, Meter, Observation

from .billing import billing_mode
from .events import KIND_API_ERROR, KIND_API_REQUEST, Event

MAX_TRACKED_MODELS = 64
PRICED_ROUTES = frozenset({"official_docs_snapshot", "subscription_included"})
# Hermes prices these from a models API, cached for an hour, so the lookup runs
# off-thread and charging starts once it lands.
FETCHED_ROUTES = frozenset({"official_models_api"})
# Hermes caches the models API for an hour, so re-reading at that cadence
# catches a rate change without adding fetches.
PRICE_TTL_SECONDS = 3600.0


def _fetchable(model: str, provider: str) -> bool:
    return billing_mode(model, provider, "") in FETCHED_ROUTES


# Reasoning tokens are already inside output_tokens, so charging them again would double count.
_BILLED = (
    ("input_tokens", "input_token_cost", "input"),
    ("output_tokens", "output_token_cost", "output"),
    ("cache_read_tokens", "cache_read_token_cost", "cache_read"),
    ("cache_write_tokens", "cache_write_token_cost", "cache_write"),
)
_PER_MILLION = 1_000_000

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
    """Look up a route's rates from data already on hand."""
    if billing_mode(model, provider, "") not in PRICED_ROUTES:
        return None
    return _lookup(model, provider)


def _lookup(model: str, provider: str) -> Rates | None:
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

    def __init__(
        self,
        meter: Meter,
        max_tracked: int = MAX_TRACKED_MODELS,
        ttl_seconds: float = PRICE_TTL_SECONDS,
    ) -> None:
        self._max_tracked = max(1, max_tracked)
        self._ttl = ttl_seconds
        self._pending: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._cost = meter.create_counter(
            "hermes.gen_ai.cost",
            unit="USD",
            description="Spend, from the published rate for the model",
        )
        self._rates: dict[tuple[str, str], Rates] = {}
        self._resolved_at: dict[tuple[str, str], float] = {}
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
        if key in self._rates:
            if self._is_stale(key):
                self._resolve_later(key)
            self._charge(self._rates[key], payload)
            return
        if key in self._skipped or len(self._rates) >= self._max_tracked:
            return
        rates = resolve(model, provider)
        if rates is None:
            if billing_mode(model, provider, "") in FETCHED_ROUTES:
                self._resolve_later(key)
            else:
                self._skipped.add(key)
            return
        self._remember(key, rates)
        self._charge(rates, payload)

    def _is_stale(self, key: tuple[str, str]) -> bool:
        return time.time() - self._resolved_at.get(key, 0.0) >= self._ttl

    def _remember(self, key: tuple[str, str], rates: Rates) -> None:
        self._rates[key] = rates
        self._resolved_at[key] = time.time()

    def _resolve_later(self, key: tuple[str, str]) -> None:
        """Re-read a rate on its own thread; the current one keeps charging meanwhile."""
        with self._lock:
            if key in self._pending:
                return
            self._pending.add(key)
        provider, model = key
        thread = threading.Thread(
            target=self._fetch, args=(key, model, provider), name="hermes-pricing", daemon=True
        )
        thread.start()

    def _fetch(self, key: tuple[str, str], model: str, provider: str) -> None:
        rates = (
            _lookup(model, provider) if _fetchable(model, provider) else resolve(model, provider)
        )
        with self._lock:
            self._pending.discard(key)
            if rates is None:
                if key not in self._rates:
                    self._skipped.add(key)
                self._resolved_at[key] = time.time()
            elif key in self._rates or len(self._rates) < self._max_tracked:
                self._remember(key, rates)

    def _charge(self, rates: Rates, payload: Mapping[str, Any]) -> None:
        usage = payload.get("usage")
        if not isinstance(usage, Mapping):
            return
        for source, rate_name, label in _BILLED:
            count = usage.get(source)
            rate = rates.values.get(rate_name)
            if isinstance(count, int) and count and rate is not None:
                self._cost.add(
                    count * rate / _PER_MILLION,
                    {
                        "gen_ai.provider.name": rates.provider,
                        "gen_ai.request.model": rates.model,
                        "gen_ai.token.type": label,
                    },
                )

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

        observe.__name__ = f"hermes_metrics_{name}"
        return observe
