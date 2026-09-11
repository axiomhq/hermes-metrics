# SPDX-License-Identifier: Apache-2.0 OR MIT
"""Per-signal destinations and the headers they carry."""

from __future__ import annotations

import pytest

from hermes_metrics.config import GENERIC_DATASET_HEADER, SIGNALS, Config

ALL_ENV = {
    "HERMES_AXIOM_TOKEN": "xaat-secret",
    "HERMES_AXIOM_TRACES_DATASET": "ds-traces",
    "HERMES_AXIOM_LOGS_DATASET": "ds-logs",
    "HERMES_AXIOM_METRICS_DATASET": "ds-metrics",
}


def _by_signal(cfg: Config) -> dict[str, object]:
    return {endpoint.signal: endpoint for endpoint in cfg.endpoints()}


def test_every_signal_has_an_endpoint_when_configured() -> None:
    assert set(_by_signal(Config.from_env(ALL_ENV))) == set(SIGNALS)


@pytest.mark.parametrize(
    ("signal", "path"),
    [("traces", "/v1/traces"), ("logs", "/v1/logs"), ("metrics", "/v1/metrics")],
)
def test_endpoint_url_targets_the_signal_route(signal: str, path: str) -> None:
    endpoint = _by_signal(Config.from_env(ALL_ENV))[signal]
    assert endpoint.url == f"https://api.axiom.co{path}"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("signal", "header", "dataset"),
    [
        ("traces", "x-axiom-traces-dataset", "ds-traces"),
        ("logs", "x-axiom-logs-dataset", "ds-logs"),
        ("metrics", "x-axiom-metrics-dataset", "ds-metrics"),
    ],
)
def test_each_signal_names_its_own_dataset_header(signal: str, header: str, dataset: str) -> None:
    headers = _by_signal(Config.from_env(ALL_ENV))[signal].headers  # type: ignore[attr-defined]
    assert headers[header] == dataset


def test_generic_dataset_header_is_never_sent() -> None:
    for endpoint in Config.from_env(ALL_ENV).endpoints():
        assert GENERIC_DATASET_HEADER not in {k.lower() for k in endpoint.headers}


def test_authorization_carries_the_token_as_a_bearer() -> None:
    for endpoint in Config.from_env(ALL_ENV).endpoints():
        assert endpoint.headers["authorization"] == "Bearer xaat-secret"


def test_only_configured_signals_get_an_endpoint() -> None:
    cfg = Config.from_env({"HERMES_AXIOM_TOKEN": "t", "HERMES_AXIOM_LOGS_DATASET": "only"})
    assert [endpoint.signal for endpoint in cfg.endpoints()] == ["logs"]


def test_region_selects_the_host() -> None:
    cfg = Config.from_env(dict(ALL_ENV, HERMES_AXIOM_DOMAIN="https://api.eu.axiom.co"))
    assert all(endpoint.url.startswith("https://api.eu.axiom.co/") for endpoint in cfg.endpoints())


def test_an_inactive_config_offers_no_endpoints() -> None:
    assert Config.from_env({}).endpoints() == ()


# Hermes logs plugin state, so a token must stay out of reprs.
def test_the_token_stays_out_of_reprs() -> None:
    cfg = Config.from_env(ALL_ENV)
    assert "xaat-secret" not in repr(cfg)
    for endpoint in cfg.endpoints():
        assert "xaat-secret" not in repr(endpoint)
