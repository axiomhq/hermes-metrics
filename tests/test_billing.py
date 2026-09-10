"""The billing route label attached to provider calls."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hermess_metrics import billing


@pytest.mark.parametrize(
    ("model", "provider", "expected"),
    [
        ("gpt-5-codex", "openai-codex", "subscription_included"),
        ("anthropic/claude-opus-5", "anthropic", "official_docs_snapshot"),
        ("gpt-5", "openai", "official_docs_snapshot"),
        ("some/model", "openrouter", "official_models_api"),
        ("mystery", "", "unknown"),
    ],
)
def test_known_routes_resolve_to_their_billing_mode(
    model: str, provider: str, expected: str
) -> None:
    assert billing.billing_mode(model, provider, "") == expected


def test_a_local_endpoint_is_unknown_rather_than_metered() -> None:
    assert billing.billing_mode("llama", "local", "http://localhost:1234") == "unknown"


def test_the_label_is_unavailable_outside_hermes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(billing, "_resolver", _raise_import_error)
    billing.billing_mode.cache_clear()
    assert billing.billing_mode("anthropic/claude-opus-5", "anthropic", "") == "unknown"
    billing.billing_mode.cache_clear()


def _raise_import_error() -> None:
    raise ImportError("hermes is not installed here")


# The label is a metric dimension, so it must come from a closed set.
@given(st.text(max_size=60), st.text(max_size=30), st.text(max_size=60))
def test_the_label_is_always_from_the_closed_set(model: str, provider: str, base_url: str) -> None:
    assert billing.billing_mode(model, provider, base_url) in billing.BILLING_MODES
