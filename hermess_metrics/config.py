"""Environment-backed configuration.

Hermes 0.19 hands plugins a context without a config accessor, so settings
arrive through the process environment, which ``~/.hermes/.env`` populates.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

ENV_PREFIX = "HERMES_AXIOM_"

ENV_TOKEN = ENV_PREFIX + "TOKEN"
ENV_DOMAIN = ENV_PREFIX + "DOMAIN"
ENV_TRACES_DATASET = ENV_PREFIX + "TRACES_DATASET"
ENV_LOGS_DATASET = ENV_PREFIX + "LOGS_DATASET"
ENV_METRICS_DATASET = ENV_PREFIX + "METRICS_DATASET"
ENV_DEBUG = ENV_PREFIX + "DEBUG"

DEFAULT_DOMAIN = "api.axiom.co"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_SCHEMES = ("https://", "http://")


def _text(env: Mapping[str, str], name: str) -> str:
    return env.get(name, "").strip()


def _host(raw: str) -> str:
    host = raw.strip().lower()
    for scheme in _SCHEMES:
        if host.startswith(scheme):
            host = host[len(scheme) :]
            break
    return host.rstrip("/").strip() or DEFAULT_DOMAIN


@dataclass(frozen=True)
class Config:
    """Resolved plugin settings and the posture they imply."""

    token: str = ""
    domain: str = DEFAULT_DOMAIN
    traces_dataset: str = ""
    logs_dataset: str = ""
    metrics_dataset: str = ""
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
            debug=_text(source, ENV_DEBUG).lower() in _TRUTHY,
        )

    @property
    def configured_signals(self) -> tuple[str, ...]:
        """Signal names whose destination dataset is known."""
        pairs = (
            ("traces", self.traces_dataset),
            ("logs", self.logs_dataset),
            ("metrics", self.metrics_dataset),
        )
        return tuple(name for name, dataset in pairs if dataset)

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

    @property
    def active(self) -> bool:
        return not self.problems()
