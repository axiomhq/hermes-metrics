"""Plugin settings and per-signal destinations, read from the environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from .dispatch import DEFAULT_CAPACITY
from .redaction import DEFAULT_LEVEL, DEFAULT_MAX_CHARS, Redactor

ENV_PREFIX = "HERMES_AXIOM_"

ENV_TOKEN = ENV_PREFIX + "TOKEN"
ENV_DOMAIN = ENV_PREFIX + "DOMAIN"
ENV_TRACES_DATASET = ENV_PREFIX + "TRACES_DATASET"
ENV_LOGS_DATASET = ENV_PREFIX + "LOGS_DATASET"
ENV_METRICS_DATASET = ENV_PREFIX + "METRICS_DATASET"
ENV_SERVICE_NAME = ENV_PREFIX + "SERVICE_NAME"
ENV_QUEUE_CAPACITY = ENV_PREFIX + "QUEUE_CAPACITY"
ENV_METRIC_INTERVAL = ENV_PREFIX + "METRIC_INTERVAL_SECONDS"
ENV_REDACTION = ENV_PREFIX + "REDACTION"
ENV_MAX_CHARS = ENV_PREFIX + "MAX_CHARS"
ENV_CONTAINER_STATS = ENV_PREFIX + "CONTAINER_STATS"
ENV_ORG = ENV_PREFIX + "ORG"
ENV_CLAIM_URL = ENV_PREFIX + "CLAIM_URL"
ENV_EXPIRES_AT = ENV_PREFIX + "EXPIRES_AT"
ENV_DEBUG = ENV_PREFIX + "DEBUG"

DEFAULT_DOMAIN = "api.axiom.co"
DEFAULT_SERVICE_NAME = "hermes"
DEFAULT_METRIC_INTERVAL = 30
MIN_METRIC_INTERVAL = 5

SIGNAL_TRACES = "traces"
SIGNAL_LOGS = "logs"
SIGNAL_METRICS = "metrics"
SIGNALS = (SIGNAL_TRACES, SIGNAL_LOGS, SIGNAL_METRICS)

# Axiom falls back to the generic header, so name only the specific one.
DATASET_HEADERS = {
    SIGNAL_TRACES: "x-axiom-traces-dataset",
    SIGNAL_LOGS: "x-axiom-logs-dataset",
    SIGNAL_METRICS: "x-axiom-metrics-dataset",
}
GENERIC_DATASET_HEADER = "x-axiom-dataset"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_SCHEMES = ("https://", "http://")


def _text(env: Mapping[str, str], name: str) -> str:
    return env.get(name, "").strip()


def _positive_int(raw: str, fallback: int) -> int:
    try:
        value = int(raw)
    except ValueError:
        return fallback
    return value if value > 0 else fallback


def _host(raw: str) -> str:
    host = raw.strip().lower()
    for scheme in _SCHEMES:
        if host.startswith(scheme):
            host = host[len(scheme) :]
            break
    return host.rstrip("/").strip() or DEFAULT_DOMAIN


@dataclass(frozen=True)
class Endpoint:
    """Where one signal goes and what it must present on arrival."""

    signal: str
    url: str
    headers: dict[str, str] = field(repr=False)


@dataclass(frozen=True)
class Config:
    """Resolved plugin settings and the posture they imply."""

    token: str = field(default="", repr=False)
    domain: str = DEFAULT_DOMAIN
    traces_dataset: str = ""
    logs_dataset: str = ""
    metrics_dataset: str = ""
    service_name: str = DEFAULT_SERVICE_NAME
    queue_capacity: int = DEFAULT_CAPACITY
    metric_interval_seconds: int = DEFAULT_METRIC_INTERVAL
    redaction: str = DEFAULT_LEVEL
    max_chars: int = DEFAULT_MAX_CHARS
    container_stats: bool = False
    org: str = ""
    claim_url: str = field(default="", repr=False)
    expires_at: str = ""
    debug: bool = False

    @staticmethod
    def env_names() -> frozenset[str]:
        return frozenset(
            {
                ENV_TOKEN,
                ENV_DOMAIN,
                ENV_TRACES_DATASET,
                ENV_LOGS_DATASET,
                ENV_METRICS_DATASET,
                ENV_SERVICE_NAME,
                ENV_QUEUE_CAPACITY,
                ENV_METRIC_INTERVAL,
                ENV_REDACTION,
                ENV_MAX_CHARS,
                ENV_CONTAINER_STATS,
                ENV_ORG,
                ENV_CLAIM_URL,
                ENV_EXPIRES_AT,
                ENV_DEBUG,
            }
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        source: Mapping[str, str] = os.environ if env is None else env
        return cls(
            token=_text(source, ENV_TOKEN),
            domain=_host(_text(source, ENV_DOMAIN)),
            traces_dataset=_text(source, ENV_TRACES_DATASET),
            logs_dataset=_text(source, ENV_LOGS_DATASET),
            metrics_dataset=_text(source, ENV_METRICS_DATASET),
            service_name=_text(source, ENV_SERVICE_NAME) or DEFAULT_SERVICE_NAME,
            queue_capacity=_positive_int(_text(source, ENV_QUEUE_CAPACITY), DEFAULT_CAPACITY),
            metric_interval_seconds=max(
                MIN_METRIC_INTERVAL,
                _positive_int(_text(source, ENV_METRIC_INTERVAL), DEFAULT_METRIC_INTERVAL),
            ),
            redaction=Redactor.for_level(_text(source, ENV_REDACTION)).level,
            max_chars=_positive_int(_text(source, ENV_MAX_CHARS), DEFAULT_MAX_CHARS),
            container_stats=_text(source, ENV_CONTAINER_STATS).lower() in _TRUTHY,
            org=_text(source, ENV_ORG),
            claim_url=_text(source, ENV_CLAIM_URL),
            expires_at=_text(source, ENV_EXPIRES_AT),
            debug=_text(source, ENV_DEBUG).lower() in _TRUTHY,
        )

    def dataset_for(self, signal: str) -> str:
        return {
            SIGNAL_TRACES: self.traces_dataset,
            SIGNAL_LOGS: self.logs_dataset,
            SIGNAL_METRICS: self.metrics_dataset,
        }.get(signal, "")

    @property
    def configured_signals(self) -> tuple[str, ...]:
        """Signal names whose destination dataset is known."""
        return tuple(signal for signal in SIGNALS if self.dataset_for(signal))

    def endpoints(self) -> tuple[Endpoint, ...]:
        """One destination per configured signal."""
        if not self.active:
            return ()
        return tuple(
            Endpoint(
                signal=signal,
                url=f"https://{self.domain}/v1/{signal}",
                headers={
                    "authorization": f"Bearer {self.token}",
                    DATASET_HEADERS[signal]: self.dataset_for(signal),
                },
            )
            for signal in self.configured_signals
        )

    def problems(self) -> tuple[str, ...]:
        """Reasons the plugin cannot ship telemetry, in the order to fix them."""
        found: list[str] = []
        if not self.token:
            found.append(f"{ENV_TOKEN} is not set")
        if not self.configured_signals:
            found.append(
                "set at least one of "
                f"{ENV_TRACES_DATASET}, {ENV_LOGS_DATASET}, {ENV_METRICS_DATASET}"
            )
        return tuple(found)

    def redactor(self) -> Redactor:
        """The content policy this configuration selects."""
        return Redactor.for_level(self.redaction, self.max_chars)

    @property
    def active(self) -> bool:
        return not self.problems()
