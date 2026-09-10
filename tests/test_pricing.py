"""Published token rates for the models in use."""

from __future__ import annotations

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


def test_a_route_needing_a_network_lookup_is_skipped(recorder_and_reader) -> None:
    recorder, reader = recorder_and_reader
    _see(recorder, "some/model", "openrouter")
    assert _points(reader) == {}


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
