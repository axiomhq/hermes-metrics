# SPDX-License-Identifier: Apache-2.0 OR MIT
"""Published token rates for the models in use."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from hermess_metrics.events import stamp
from hermess_metrics.pricing import PriceRecorder, resolve

MODEL = "claude-opus-4-5"


@pytest.fixture
def recorder_and_reader():
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    yield PriceRecorder(provider.get_meter("hermess-metrics")), reader


def _points(reader: InMemoryMetricReader) -> dict[str, list[Any]]:
    data = reader.get_metrics_data()
    found: dict[str, list[Any]] = {}
    for resource_metric in data.resource_metrics if data else []:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                found.setdefault(metric.name, []).extend(metric.data.data_points)
    return found


def _see(recorder: PriceRecorder, model: str, provider: str) -> None:
    recorder.handle(stamp("api_request", {"model": model, "provider": provider}))


def test_nothing_is_published_before_a_model_is_seen(recorder_and_reader) -> None:
    _, reader = recorder_and_reader
    assert _points(reader) == {}


def test_a_priced_model_publishes_all_four_rates(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _see(recorder, MODEL, "anthropic")
    points = _points(reader)
    assert points["hermes.gen_ai.input_token_cost"][0].value == pytest.approx(5.0)
    assert points["hermes.gen_ai.output_token_cost"][0].value == pytest.approx(25.0)
    assert points["hermes.gen_ai.cache_read_token_cost"][0].value == pytest.approx(0.5)
    assert points["hermes.gen_ai.cache_write_token_cost"][0].value == pytest.approx(6.25)


def test_the_rate_carries_its_model_and_version(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _see(recorder, MODEL, "anthropic")
    attributes = _points(reader)["hermes.gen_ai.input_token_cost"][0].attributes
    assert attributes["gen_ai.request.model"] == MODEL
    assert attributes["gen_ai.provider.name"] == "anthropic"
    assert attributes["hermes.pricing_version"] == "anthropic-pricing-2026-05"


def test_a_provider_prefixed_model_still_resolves(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _see(recorder, f"anthropic/{MODEL}", "anthropic")
    assert _points(reader)["hermes.gen_ai.input_token_cost"][0].value == pytest.approx(5.0)


def test_a_subscription_included_route_is_priced_at_zero(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _see(recorder, "gpt-5-codex", "openai-codex")
    points = _points(reader)
    assert points["hermes.gen_ai.input_token_cost"][0].value == 0.0
    assert points["hermes.gen_ai.output_token_cost"][0].value == 0.0


def test_a_models_api_route_is_resolved_off_thread(recorder_and_reader, monkeypatch) -> None:
    """OpenRouter rates come from a fetch, so they must never block the export path."""
    from hermess_metrics import pricing

    started = threading.Event()
    release = threading.Event()

    def slow_lookup(model: str, provider: str) -> Any:
        started.set()
        release.wait(5.0)
        return pricing.Rates(
            provider=provider, model=model, version="openrouter", values={"input_token_cost": 2.0}
        )

    monkeypatch.setattr(pricing, "_lookup", slow_lookup)
    recorder, reader = recorder_and_reader
    _see(recorder, "some/model", "openrouter")
    assert started.wait(5.0)
    assert _points(reader) == {}
    release.set()
    for _ in range(100):
        if recorder.tracked:
            break
        time.sleep(0.02)
    assert recorder.tracked == 1
    _call(recorder, "some/model", "openrouter", {"input_tokens": 1_000_000})
    assert _points(reader)["hermes.gen_ai.cost"][0].value == pytest.approx(2.0)


def test_a_models_api_route_is_only_fetched_once(recorder_and_reader, monkeypatch) -> None:
    from hermess_metrics import pricing

    calls: list[tuple[str, str]] = []

    def counting_lookup(model: str, provider: str) -> Any:
        calls.append((model, provider))
        time.sleep(0.05)
        return None

    monkeypatch.setattr(pricing, "_lookup", counting_lookup)
    recorder, reader = recorder_and_reader
    for _ in range(10):
        _see(recorder, "some/model", "openrouter")
    time.sleep(0.3)
    assert len(calls) == 1


def test_a_failed_fetch_is_not_retried_forever(recorder_and_reader, monkeypatch) -> None:
    from hermess_metrics import pricing

    calls: list[str] = []
    monkeypatch.setattr(pricing, "_lookup", lambda model, provider: calls.append(model) or None)
    recorder, reader = recorder_and_reader
    _see(recorder, "some/model", "openrouter")
    time.sleep(0.2)
    _see(recorder, "some/model", "openrouter")
    time.sleep(0.2)
    assert len(calls) == 1


def test_an_unpriced_model_publishes_nothing(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _see(recorder, "no-such-model", "anthropic")
    assert _points(reader) == {}


def test_a_model_is_resolved_once_however_often_it_is_seen(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    for _ in range(50):
        _see(recorder, MODEL, "anthropic")
    assert len(_points(reader)["hermes.gen_ai.input_token_cost"]) == 1


def test_the_tracked_model_set_is_bounded() -> None:
    """Feed more priced routes than the cap and watch it stop."""
    from agent.usage_pricing import _OFFICIAL_DOCS_PRICING

    provider = MeterProvider(metric_readers=[InMemoryMetricReader()])
    recorder = PriceRecorder(provider.get_meter("hermess-metrics"), max_tracked=4)
    priced = [(p, m) for p, m in sorted(_OFFICIAL_DOCS_PRICING) if resolve(m, p) is not None]
    assert len(priced) > 4
    for route_provider, model in priced:
        _see(recorder, model, route_provider)
    assert recorder.tracked == 4


def test_kinds_this_recorder_does_not_consume_are_ignored(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    recorder.handle(stamp("session_start", {"session_id": "s"}))
    recorder.handle("not an event")
    assert _points(reader) == {}


def test_a_pricing_lookup_that_raises_is_treated_as_unpriced(
    recorder_and_reader, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermess_metrics import pricing

    monkeypatch.setattr(pricing, "_entry_for", _raise)
    recorder, reader = recorder_and_reader
    _see(recorder, MODEL, "anthropic")
    assert _points(reader) == {}


def _raise(model: str, provider: str) -> Any:
    raise RuntimeError("pricing table unavailable")


def test_an_event_without_a_model_is_ignored(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    recorder.handle(stamp("api_request", {"provider": "anthropic"}))
    assert _points(reader) == {}


def test_a_route_with_an_empty_rate_set_is_treated_as_unpriced(
    recorder_and_reader, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermess_metrics import pricing

    monkeypatch.setattr(pricing, "_entry_for", lambda model, provider: object())
    recorder, reader = recorder_and_reader
    _see(recorder, MODEL, "anthropic")
    assert _points(reader) == {}


USAGE = {
    "input_tokens": 1_000_000,
    "output_tokens": 100_000,
    "cache_read_tokens": 2_000_000,
    "cache_write_tokens": 400_000,
    "reasoning_tokens": 50_000,
}


def _call(recorder: PriceRecorder, model: str, provider: str, usage: dict[str, Any]) -> None:
    recorder.handle(stamp("api_request", {"model": model, "provider": provider, "usage": usage}))


def test_spend_is_charged_per_token_bucket(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _call(recorder, MODEL, "anthropic", USAGE)
    by_type = {
        point.attributes["gen_ai.token.type"]: point.value
        for point in _points(reader)["hermes.gen_ai.cost"]
    }
    assert by_type["input"] == pytest.approx(5.00)
    assert by_type["output"] == pytest.approx(2.50)
    assert by_type["cache_read"] == pytest.approx(1.00)
    assert by_type["cache_write"] == pytest.approx(2.50)


def test_reasoning_tokens_are_not_charged_again(recorder_and_reader) -> None:
    """They are already inside output_tokens."""
    recorder, reader = recorder_and_reader
    _call(recorder, MODEL, "anthropic", USAGE)
    types = {
        point.attributes["gen_ai.token.type"] for point in _points(reader)["hermes.gen_ai.cost"]
    }
    assert "reasoning" not in types


def test_spend_accumulates_across_calls(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _call(recorder, MODEL, "anthropic", {"input_tokens": 1_000_000})
    _call(recorder, MODEL, "anthropic", {"input_tokens": 1_000_000})
    point = _points(reader)["hermes.gen_ai.cost"][0]
    assert point.value == pytest.approx(10.00)


def test_a_subscription_route_charges_zero(recorder_and_reader) -> None:
    """Zero is the answer, and saying it beats leaving a gap in the series."""
    recorder, reader = recorder_and_reader
    _call(recorder, "gpt-5-codex", "openai-codex", {"input_tokens": 1_000_000})
    assert _points(reader)["hermes.gen_ai.cost"][0].value == 0.0


def test_an_unpriced_model_is_not_charged(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _call(recorder, "no-such-model", "anthropic", {"input_tokens": 1_000_000})
    assert "hermes.gen_ai.cost" not in _points(reader)


def test_a_call_without_usage_is_not_charged(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _see(recorder, MODEL, "anthropic")
    assert "hermes.gen_ai.cost" not in _points(reader)


def test_a_stale_rate_is_refreshed_and_the_new_one_charged(monkeypatch) -> None:
    """A rate change has to show up, or the price-rise alert can never fire."""
    from hermess_metrics import pricing

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    recorder = PriceRecorder(provider.get_meter("x"), ttl_seconds=0.0)
    versions = iter(["v1", "v2"])

    def changing(model: str, provider_name: str) -> Any:
        return pricing.Rates(
            provider=provider_name,
            model=model,
            version=next(versions, "v2"),
            values={"input_token_cost": 1.0 if provider_name == "seen" else 9.0},
        )

    monkeypatch.setattr(pricing, "resolve", changing)
    monkeypatch.setattr(pricing, "_lookup", changing)
    recorder.handle(stamp("api_request", {"model": "m", "provider": "openrouter"}))
    for _ in range(100):
        if recorder.tracked:
            break
        time.sleep(0.02)
    recorder.handle(stamp("api_request", {"model": "m", "provider": "openrouter"}))
    time.sleep(0.2)
    point = _points(reader)["hermes.gen_ai.input_token_cost"][0]
    assert point.attributes["hermes.pricing_version"] == "v2"


def test_a_fresh_rate_is_not_refetched(monkeypatch) -> None:
    from hermess_metrics import pricing

    provider = MeterProvider(metric_readers=[InMemoryMetricReader()])
    recorder = PriceRecorder(provider.get_meter("x"), ttl_seconds=3600.0)
    recorder.handle(stamp("api_request", {"model": MODEL, "provider": "anthropic"}))
    assert recorder.tracked == 1
    calls: list[str] = []
    monkeypatch.setattr(pricing, "_lookup", lambda model, p: calls.append(model) or None)
    monkeypatch.setattr(pricing, "resolve", lambda model, p: calls.append(model) or None)
    for _ in range(5):
        recorder.handle(stamp("api_request", {"model": MODEL, "provider": "anthropic"}))
    time.sleep(0.15)
    assert calls == []


def test_a_refresh_that_fails_keeps_the_rate_it_had(monkeypatch) -> None:
    from hermess_metrics import pricing

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    recorder = PriceRecorder(provider.get_meter("x"), ttl_seconds=0.0)
    recorder.handle(stamp("api_request", {"model": MODEL, "provider": "anthropic"}))
    assert recorder.tracked == 1
    monkeypatch.setattr(pricing, "resolve", lambda model, p: None)
    monkeypatch.setattr(pricing, "_lookup", lambda model, p: None)
    recorder.handle(stamp("api_request", {"model": MODEL, "provider": "anthropic"}))
    time.sleep(0.2)
    assert recorder.tracked == 1
    assert _points(reader)["hermes.gen_ai.input_token_cost"][0].value == pytest.approx(5.0)
