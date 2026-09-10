"""Ships Hermes agent-loop telemetry to Axiom as OpenTelemetry signals.

Hermes calls :func:`register` once when the plugin loads, either from a
drop-in directory under ``~/.hermes/plugins/`` or through the
``hermes_agent.plugins`` entry point.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from .config import Config

__version__ = "0.1.0"
__all__ = ["Config", "register", "__version__"]

logger = logging.getLogger(__name__)


def register(ctx: Any, env: Mapping[str, str] | None = None) -> Config:
    """Resolve settings and report the posture Hermes loaded us with.

    Returns the resolved config so callers and tests can inspect what the
    environment produced.
    """
    config = Config.from_env(env)

    if config.active:
        logger.info(
            "hermess-metrics loaded: domain=%s signals=%s",
            config.domain,
            ", ".join(config.configured_signals),
        )
    else:
        logger.warning(
            "hermess-metrics is enabled but idle: %s",
            "; ".join(config.problems()),
        )

    return config
