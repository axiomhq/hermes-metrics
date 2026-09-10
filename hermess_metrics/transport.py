"""Builds one OTLP exporter per configured signal."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .config import SIGNAL_LOGS, SIGNAL_METRICS, SIGNAL_TRACES, Config

EXPORT_TIMEOUT_SECONDS = 10.0

SDK_HINT = "the opentelemetry SDK is required: pip install 'hermess-metrics[otlp]'"


class TransportUnavailable(RuntimeError):
    """Raised when exporters cannot be built for the current environment."""


@dataclass(frozen=True)
class Transport:
    """Exporters keyed by signal, sharing one resource identity."""

    exporters: Mapping[str, Any] = field(repr=False)
    resource: Any = field(repr=False)


def _load_exporters() -> dict[str, Any]:
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return {
        SIGNAL_TRACES: OTLPSpanExporter,
        SIGNAL_LOGS: OTLPLogExporter,
        SIGNAL_METRICS: OTLPMetricExporter,
    }


def sdk_hint() -> str | None:
    """Describe what is missing, or ``None`` when exporters can be built."""
    try:
        _load_exporters()
    except ImportError:
        return SDK_HINT
    return None


def _cumulative_temporality() -> dict[type, Any]:
    """Axiom detects counter restarts from cumulative series."""
    from opentelemetry.sdk.metrics import (
        Counter,
        Histogram,
        ObservableCounter,
        ObservableGauge,
        ObservableUpDownCounter,
        UpDownCounter,
    )
    from opentelemetry.sdk.metrics.export import AggregationTemporality

    cumulative = AggregationTemporality.CUMULATIVE
    return {
        Counter: cumulative,
        UpDownCounter: cumulative,
        Histogram: cumulative,
        ObservableCounter: cumulative,
        ObservableUpDownCounter: cumulative,
        ObservableGauge: cumulative,
    }


def _resource(config: Config, distro_version: str) -> Any:
    from opentelemetry.sdk.resources import Resource

    return Resource.create(
        {
            "service.name": config.service_name,
            "telemetry.distro.name": "hermess-metrics",
            "telemetry.distro.version": distro_version,
        }
    )


def build(config: Config) -> Transport:
    """Construct one exporter per configured signal."""
    from . import __version__

    if not config.active:
        raise TransportUnavailable("; ".join(config.problems()))

    try:
        exporter_types = _load_exporters()
    except ImportError as exc:
        raise TransportUnavailable(SDK_HINT) from exc

    exporters: dict[str, Any] = {}
    for endpoint in config.endpoints():
        extra: dict[str, Any] = {}
        if endpoint.signal == SIGNAL_METRICS:
            extra["preferred_temporality"] = _cumulative_temporality()
        exporters[endpoint.signal] = exporter_types[endpoint.signal](
            endpoint=endpoint.url,
            headers=dict(endpoint.headers),
            timeout=EXPORT_TIMEOUT_SECONDS,
            **extra,
        )

    return Transport(exporters=exporters, resource=_resource(config, __version__))
