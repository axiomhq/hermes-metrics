"""Settings read from the environment."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from hermess_metrics.config import ENV_PREFIX, Config

FULL_ENV = {
    "HERMES_AXIOM_TOKEN": "xaat-secret",
    "HERMES_AXIOM_DOMAIN": "api.axiom.co",
    "HERMES_AXIOM_TRACES_DATASET": "hermes-traces",
    "HERMES_AXIOM_LOGS_DATASET": "hermes-logs",
    "HERMES_AXIOM_METRICS_DATASET": "hermes-metrics",
}


def test_empty_env_is_inactive_and_reports_why() -> None:
    cfg = Config.from_env({})
    assert not cfg.active
    problems = " ".join(cfg.problems())
    assert "HERMES_AXIOM_TOKEN" in problems
    assert "DATASET" in problems


def test_full_env_is_active_with_no_problems() -> None:
    cfg = Config.from_env(FULL_ENV)
    assert cfg.active
    assert cfg.problems() == ()
    assert cfg.token == "xaat-secret"
    assert cfg.traces_dataset == "hermes-traces"
    assert cfg.logs_dataset == "hermes-logs"
    assert cfg.metrics_dataset == "hermes-metrics"


def test_one_dataset_is_enough_to_activate() -> None:
    env = {"HERMES_AXIOM_TOKEN": "t", "HERMES_AXIOM_TRACES_DATASET": "only-traces"}
    cfg = Config.from_env(env)
    assert cfg.active
    assert cfg.logs_dataset == ""
    assert cfg.metrics_dataset == ""


def test_token_without_any_dataset_is_inactive() -> None:
    cfg = Config.from_env({"HERMES_AXIOM_TOKEN": "t"})
    assert not cfg.active


def test_domain_defaults_to_us_region() -> None:
    assert Config.from_env({}).domain == "api.axiom.co"


def test_domain_is_normalised_to_a_bare_host() -> None:
    for raw in ("https://api.eu.axiom.co", "http://api.eu.axiom.co/", "  API.EU.Axiom.co/  "):
        assert Config.from_env({"HERMES_AXIOM_DOMAIN": raw}).domain == "api.eu.axiom.co"


def test_blank_values_are_treated_as_absent() -> None:
    env = dict(FULL_ENV, HERMES_AXIOM_TOKEN="   ")
    cfg = Config.from_env(env)
    assert cfg.token == ""
    assert not cfg.active


def test_debug_flag_accepts_common_truthy_spellings() -> None:
    for raw in ("1", "true", "TRUE", "yes", "on"):
        assert Config.from_env({"HERMES_AXIOM_DEBUG": raw}).debug
    for raw in ("", "0", "false", "no", "off", "banana"):
        assert not Config.from_env({"HERMES_AXIOM_DEBUG": raw}).debug


def test_every_env_name_carries_the_shared_prefix() -> None:
    assert all(name.startswith(ENV_PREFIX) for name in Config.env_names())


# Hermes swallows a raising plugin, so parsing must be total.
@given(
    st.dictionaries(
        keys=st.sampled_from(sorted(Config.env_names())),
        values=st.text(max_size=200),
        max_size=10,
    )
)
def test_from_env_is_total(env: dict[str, str]) -> None:
    cfg = Config.from_env(env)
    assert cfg.active == (cfg.problems() == ())


def test_queue_capacity_defaults_and_accepts_an_override() -> None:
    assert Config.from_env({}).queue_capacity == 2048
    assert Config.from_env({"HERMES_AXIOM_QUEUE_CAPACITY": "64"}).queue_capacity == 64


def test_a_nonsense_queue_capacity_falls_back_to_the_default() -> None:
    for raw in ("0", "-5", "lots", "3.5", ""):
        assert Config.from_env({"HERMES_AXIOM_QUEUE_CAPACITY": raw}).queue_capacity == 2048


def test_redaction_defaults_to_the_most_conservative_level() -> None:
    assert Config.from_env({}).redaction == "metadata"
    assert Config.from_env({}).redactor().level == "metadata"


def test_redaction_level_is_selectable() -> None:
    assert Config.from_env({"HERMES_AXIOM_REDACTION": "full"}).redaction == "full"
    assert Config.from_env({"HERMES_AXIOM_REDACTION": "tools"}).redaction == "tools"


def test_an_unknown_redaction_level_does_not_widen_capture() -> None:
    assert Config.from_env({"HERMES_AXIOM_REDACTION": "everything"}).redaction == "metadata"


def test_max_chars_is_configurable_and_falls_back_when_nonsense() -> None:
    assert Config.from_env({"HERMES_AXIOM_MAX_CHARS": "500"}).max_chars == 500
    assert Config.from_env({"HERMES_AXIOM_MAX_CHARS": "-1"}).max_chars == 12000


def test_container_stats_are_off_unless_asked_for() -> None:
    assert Config.from_env({}).container_stats is False
    assert Config.from_env({"HERMES_AXIOM_CONTAINER_STATS": "true"}).container_stats is True
