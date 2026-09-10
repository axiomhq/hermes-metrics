"""Ships Hermes agent-loop telemetry to Axiom as OpenTelemetry signals.

Hermes calls :func:`register` once when the plugin loads, either from a
drop-in directory under ``~/.hermes/plugins/`` or through the
``hermes_agent.plugins`` entry point.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from .config import Config

__version__ = "0.1.0"
__all__ = ["Config", "register", "__version__"]

logger = logging.getLogger(__name__)


def _default_factory(config: Config) -> Any:
    from .runtime import Runtime

    return Runtime.start(config)


def register(
    ctx: Any,
    env: Mapping[str, str] | None = None,
    runtime_factory: Callable[[Config], Any] = _default_factory,
) -> Config:
    """Resolve settings, start the exporters, and subscribe to the hooks.

    Returns the resolved config so callers and tests can inspect what the
    environment produced.
    """
    from . import transport

    config = Config.from_env(env)

    if not config.active:
        logger.warning(
            "hermess-metrics is enabled but idle: %s",
            "; ".join(config.problems()),
        )
        return config

    hint = transport.sdk_hint()
    if hint is not None:
        logger.warning("hermess-metrics is enabled but idle: %s", hint)
        return config

    try:
        runtime = runtime_factory(config)
        hooks = runtime.register_hooks(ctx)
    except Exception:
        logger.warning("hermess-metrics could not start; Hermes continues", exc_info=True)
        return config

    logger.info(
        "hermess-metrics loaded: domain=%s signals=%s redaction=%s hooks=%d",
        config.domain,
        ", ".join(config.configured_signals),
        config.redaction,
        len(hooks),
    )
    return config
