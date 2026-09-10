"""What content leaves the machine at each level."""

from __future__ import annotations

import json

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from hermess_metrics.redaction import (
    CLASS_MESSAGES,
    CLASS_METADATA,
    CLASS_TOOL_IO,
    LEVEL_FULL,
    LEVEL_METADATA,
    LEVEL_TOOLS,
    LEVELS,
    MASK,
    Redactor,
)

SECRET = "sk-live-do-not-ship-this"

json_values = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=40)),
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(min_size=1, max_size=12), children, max_size=4),
    ),
    max_leaves=12,
)


def test_the_default_level_is_the_most_conservative() -> None:
    assert Redactor().level == LEVEL_METADATA
    assert LEVELS[0] == LEVEL_METADATA


@pytest.mark.parametrize("level", LEVELS)
def test_metadata_survives_at_every_level(level: str) -> None:
    redactor = Redactor.for_level(level)
    assert redactor.allows(CLASS_METADATA)
    assert redactor.text("claude-opus-5", CLASS_METADATA) == "claude-opus-5"


def test_the_metadata_level_ships_no_content() -> None:
    redactor = Redactor.for_level(LEVEL_METADATA)
    assert redactor.text("hello", CLASS_MESSAGES) is None
    assert redactor.text("hello", CLASS_TOOL_IO) is None


def test_the_tools_level_ships_tool_io_but_not_messages() -> None:
    redactor = Redactor.for_level(LEVEL_TOOLS)
    assert redactor.text("ls -la", CLASS_TOOL_IO) == "ls -la"
    assert redactor.text("what is my password", CLASS_MESSAGES) is None


def test_the_full_level_ships_everything() -> None:
    redactor = Redactor.for_level(LEVEL_FULL)
    assert redactor.text("hello", CLASS_MESSAGES) == "hello"
    assert redactor.text("ls -la", CLASS_TOOL_IO) == "ls -la"


def test_an_unknown_level_falls_back_to_the_most_conservative() -> None:
    for raw in ("", "everything", "metadata-ish", "banana"):
        assert Redactor.for_level(raw).level == LEVEL_METADATA


def test_level_names_tolerate_case_and_surrounding_space() -> None:
    assert Redactor.for_level("  FULL ").level == LEVEL_FULL


def test_text_is_capped_and_marked_as_truncated() -> None:
    redactor = Redactor.for_level(LEVEL_FULL, max_chars=10)
    out = redactor.text("x" * 500, CLASS_MESSAGES)
    assert out is not None
    assert len(out) <= 10


def test_credential_keys_are_masked_even_at_the_full_level() -> None:
    redactor = Redactor.for_level(LEVEL_FULL)
    out = redactor.structure(
        {"api_key": SECRET, "authorization": SECRET, "model": "claude-opus-5"},
        CLASS_TOOL_IO,
    )
    assert out == {"api_key": MASK, "authorization": MASK, "model": "claude-opus-5"}


def test_token_count_fields_are_not_mistaken_for_credentials() -> None:
    redactor = Redactor.for_level(LEVEL_FULL)
    out = redactor.structure({"input_tokens": 12, "output_tokens": 3}, CLASS_METADATA)
    assert out == {"input_tokens": 12, "output_tokens": 3}


def test_structure_is_withheld_when_the_level_forbids_the_class() -> None:
    assert Redactor.for_level(LEVEL_METADATA).structure({"a": 1}, CLASS_TOOL_IO) is None


def test_deep_nesting_is_bounded() -> None:
    deep: object = "leaf"
    for _ in range(50):
        deep = {"next": deep}
    out = Redactor.for_level(LEVEL_FULL).structure(deep, CLASS_TOOL_IO)
    assert json.dumps(out)


def test_long_sequences_are_bounded() -> None:
    out = Redactor.for_level(LEVEL_FULL).structure(list(range(10_000)), CLASS_TOOL_IO)
    assert isinstance(out, list)
    assert len(out) <= 200


# The level governs content; credentials are masked at every level.
@given(json_values, st.integers(min_value=0, max_value=5))
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=150)
def test_a_secret_never_survives_at_any_level(payload: object, depth: int) -> None:
    nested: object = {"api_key": SECRET, "payload": payload}
    for _ in range(depth):
        nested = {"wrapper": nested}
    for level in LEVELS:
        out = Redactor.for_level(level).structure(nested, CLASS_TOOL_IO)
        assert SECRET not in json.dumps(out)


# Attribute values ride into OTLP, which carries no arbitrary Python objects.
@given(json_values)
def test_scrubbed_output_is_always_serialisable(payload: object) -> None:
    out = Redactor.for_level(LEVEL_FULL).structure(payload, CLASS_TOOL_IO)
    json.dumps(out)


@given(st.text(max_size=2000), st.integers(min_value=1, max_value=50))
def test_text_never_exceeds_the_cap(value: str, cap: int) -> None:
    out = Redactor.for_level(LEVEL_FULL, max_chars=cap).text(value, CLASS_MESSAGES)
    assert out is not None
    assert len(out) <= cap


def test_non_string_keys_are_stringified_rather_than_treated_as_secrets() -> None:
    out = Redactor.for_level(LEVEL_FULL).structure({1: "one", None: "none"}, CLASS_TOOL_IO)
    assert out == {"1": "one", "None": "none"}


def test_objects_outside_the_json_types_become_capped_text() -> None:
    class Widget:
        def __repr__(self) -> str:
            return "Widget(" + "y" * 500 + ")"

    out = Redactor.for_level(LEVEL_FULL, max_chars=20).structure(
        {"widget": Widget()}, CLASS_TOOL_IO
    )
    assert isinstance(out, dict)
    assert isinstance(out["widget"], str)
    assert len(out["widget"]) <= 20
    json.dumps(out)
