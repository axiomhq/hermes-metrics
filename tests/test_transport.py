"""Exporter construction.

Axiom's metrics route accepts protobuf only, so every signal uses the
protobuf-over-HTTP exporter rather than mixing encodings.
"""

from __future__ import annotations

import pytest

from hermess_metrics import transport
from hermess_metrics.config import Config

ALL_ENV = {
    "HERMES_AXIOM_TOKEN": "xaat-secret",
    "HERMES_AXIOM_TRACES_DATASET": "ds-traces",
    "HERMES_AXIOM_LOGS_DATASET": "ds-logs",
    "HERMES_AXIOM_METRICS_DATASET": "ds-metrics",
}


def test_sdk_is_present_in_this_environment() -> None:
    assert transport.sdk_hint() is None


def test_one_exporter_per_configured_signal() -> None:
    built = transport.build(Config.from_env(ALL_ENV))
    assert set(built.exporters) == {"traces", "logs", "metrics"}


def test_unconfigured_signals_get_no_exporter() -> None:
    cfg = Config.from_env({"HERMES_AXIOM_TOKEN": "t", "HERMES_AXIOM_TRACES_DATASET": "only"})
    assert set(transport.build(cfg).exporters) == {"traces"}


def test_every_exporter_speaks_protobuf_over_http() -> None:
    built = transport.build(Config.from_env(ALL_ENV))
    for exporter in built.exporters.values():
        assert type(exporter).__module__.startswith("opentelemetry.exporter.otlp.proto.http")


@pytest.mark.parametrize(
    ("signal", "path"),
    [("traces", "/v1/traces"), ("logs", "/v1/logs"), ("metrics", "/v1/metrics")],
)
def test_exporter_points_at_the_signal_route(signal: str, path: str) -> None:
    built = transport.build(Config.from_env(ALL_ENV))
    assert built.exporters[signal]._endpoint == f"https://api.axiom.co{path}"


@pytest.mark.parametrize(
    ("signal", "header"),
    [
        ("traces", "x-axiom-traces-dataset"),
        ("logs", "x-axiom-logs-dataset"),
        ("metrics", "x-axiom-metrics-dataset"),
    ],
)
def test_exporter_carries_its_dataset_header(signal: str, header: str) -> None:
    session_headers = built_headers(signal)
    assert header in session_headers


def built_headers(signal: str) -> dict[str, str]:
    built = transport.build(Config.from_env(ALL_ENV))
    return dict(built.exporters[signal]._session.headers)


def test_resource_identifies_the_service_and_this_plugin() -> None:
    attributes = transport.build(Config.from_env(ALL_ENV)).resource.attributes
    assert attributes["service.name"] == "hermes"
    assert attributes["telemetry.distro.name"] == "hermess-metrics"


def test_service_name_is_configurable() -> None:
    cfg = Config.from_env(dict(ALL_ENV, HERMES_AXIOM_SERVICE_NAME="hermes-prod"))
    assert transport.build(cfg).resource.attributes["service.name"] == "hermes-prod"


# Axiom detects counter restarts from cumulative series; delta temporality
# would strip the signal the backend relies on.
def test_counters_export_cumulatively() -> None:
    from opentelemetry.sdk.metrics import Counter
    from opentelemetry.sdk.metrics.export import AggregationTemporality

    exporter = transport.build(Config.from_env(ALL_ENV)).exporters["metrics"]
    assert exporter._preferred_temporality[Counter] is AggregationTemporality.CUMULATIVE


def test_a_missing_sdk_is_reported_rather_than_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport, "_load_exporters", _raise_import_error)
    assert "opentelemetry" in (transport.sdk_hint() or "")
    with pytest.raises(transport.TransportUnavailable):
        transport.build(Config.from_env(ALL_ENV))


def _raise_import_error() -> None:
    raise ImportError("no opentelemetry here")


def test_an_inactive_config_builds_nothing() -> None:
    with pytest.raises(transport.TransportUnavailable):
        transport.build(Config.from_env({}))
