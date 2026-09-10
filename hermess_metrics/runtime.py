"""Assembles providers, recorders and hook subscriptions."""

from __future__ import annotations

import atexit
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .approvals import ApprovalRecorder
from .config import SIGNAL_LOGS, SIGNAL_METRICS, SIGNAL_TRACES, Config
from .containers import ContainerStats
from .dispatch import Dispatcher
from .events import (
    KIND_API_ERROR,
    KIND_API_REQUEST,
    KIND_APPROVAL_REQUEST,
    KIND_APPROVAL_RESPONSE,
    KIND_DIAGNOSTIC,
    KIND_SESSION_END,
    KIND_SESSION_START,
    KIND_SUBAGENT_START,
    KIND_SUBAGENT_STOP,
    KIND_TOOL_CALL,
    KIND_TURN_END,
    KIND_TURN_START,
    stamp,
)
from .health import HealthMetrics
from .inventory import ToolInventory
from .logs import LogRecorder
from .metrics import MetricRecorder
from .pricing import PriceRecorder
from .skills import SkillInventory
from .subagents import SubagentRecorder
from .traces import TraceRecorder
from .transport import Transport
from .transport import build as build_transport
from .turns import TurnRecorder

# The payload schema this field mapping was written against.
EXPECTED_SCHEMA = "hermes.observer.v1"
SCHEMA_FIELD = "telemetry_schema_version"

HOOK_KINDS = {
    "on_session_start": KIND_SESSION_START,
    "on_session_end": KIND_SESSION_END,
    "pre_llm_call": KIND_TURN_START,
    "post_llm_call": KIND_TURN_END,
    "post_api_request": KIND_API_REQUEST,
    "api_request_error": KIND_API_ERROR,
    "post_tool_call": KIND_TOOL_CALL,
    "pre_approval_request": KIND_APPROVAL_REQUEST,
    "post_approval_response": KIND_APPROVAL_RESPONSE,
    "subagent_start": KIND_SUBAGENT_START,
    "subagent_stop": KIND_SUBAGENT_STOP,
}
FLUSH_HOOK = "on_session_finalize"
SHUTDOWN_TIMEOUT = 5.0

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    """Owns everything that must be flushed and shut down."""

    config: Config
    dispatcher: Dispatcher[Any]
    recorders: tuple[Any, ...]
    providers: tuple[Any, ...] = field(default_factory=tuple)
    _schema_reported: bool = False
    _stopped: bool = False

    @classmethod
    def start(cls, config: Config) -> Runtime:
        return cls.build(config, build_transport(config))

    @classmethod
    def build(cls, config: Config, transport: Transport) -> Runtime:
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        dispatcher: Dispatcher[Any] = Dispatcher(capacity=config.queue_capacity)
        recorders: list[Any] = []
        providers: list[Any] = []
        redactor = config.redactor()

        if SIGNAL_TRACES in transport.exporters:
            provider = TracerProvider(resource=transport.resource)
            provider.add_span_processor(BatchSpanProcessor(transport.exporters[SIGNAL_TRACES]))
            providers.append(provider)
            recorders.append(TraceRecorder(provider.get_tracer(__package__), redactor))

        if SIGNAL_METRICS in transport.exporters:
            reader = PeriodicExportingMetricReader(
                transport.exporters[SIGNAL_METRICS],
                export_interval_millis=config.metric_interval_seconds * 1000,
            )
            provider_m = MeterProvider(resource=transport.resource, metric_readers=[reader])
            providers.append(provider_m)
            meter = provider_m.get_meter(__package__)
            recorders.append(MetricRecorder(meter))
            recorders.append(PriceRecorder(meter))
            recorders.append(ToolInventory(meter))
            recorders.append(SkillInventory(meter))
            recorders.append(ApprovalRecorder(meter))
            recorders.append(SubagentRecorder(meter))
            recorders.append(TurnRecorder(meter))
            HealthMetrics(meter, dispatcher)
            if config.container_stats:
                ContainerStats(meter)

        if SIGNAL_LOGS in transport.exporters:
            provider_l = LoggerProvider(resource=transport.resource)
            provider_l.add_log_record_processor(
                BatchLogRecordProcessor(transport.exporters[SIGNAL_LOGS])
            )
            providers.append(provider_l)
            recorders.append(LogRecorder(provider_l.get_logger(__package__), redactor))

        runtime = cls(
            config=config,
            dispatcher=dispatcher,
            recorders=tuple(recorders),
            providers=tuple(providers),
        )
        dispatcher.start(runtime._fan_out)
        atexit.register(runtime.shutdown)
        return runtime

    def _fan_out(self, event: Any) -> None:
        for recorder in self.recorders:
            recorder.handle(event)

    def _check_schema(self, payload: Mapping[str, Any]) -> None:
        version = payload.get(SCHEMA_FIELD)
        if version in (None, EXPECTED_SCHEMA) or self._schema_reported:
            return
        self._schema_reported = True
        logger.warning(
            "hermess-metrics expects hook payload schema %s but Hermes sent %s; "
            "the field mapping may be stale",
            EXPECTED_SCHEMA,
            version,
        )
        self.dispatcher.submit(
            stamp(
                KIND_DIAGNOSTIC,
                {
                    "message": "hook payload schema changed",
                    "hermes.expected_schema": EXPECTED_SCHEMA,
                    "hermes.observed_schema": str(version),
                },
            )
        )

    def _callback(self, kind: str) -> Any:
        def observe(**payload: Any) -> None:
            self._check_schema(payload)
            self.dispatcher.submit(stamp(kind, payload))

        observe.__name__ = f"hermess_metrics_{kind}"
        return observe

    def register_hooks(self, ctx: Any) -> tuple[str, ...]:
        for hook, kind in HOOK_KINDS.items():
            ctx.register_hook(hook, self._callback(kind))
        ctx.register_hook(FLUSH_HOOK, self._on_finalize)
        return (*HOOK_KINDS, FLUSH_HOOK)

    def _on_finalize(self, **payload: Any) -> None:
        self.flush(SHUTDOWN_TIMEOUT)

    def flush(self, timeout: float = SHUTDOWN_TIMEOUT) -> bool:
        """Drain the queue, then push whatever the providers still hold."""
        drained = self.dispatcher.flush(timeout)
        for provider in self.providers:
            provider.force_flush(int(timeout * 1000))
        return drained

    def shutdown(self, timeout: float = SHUTDOWN_TIMEOUT) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.flush(timeout)
        self.dispatcher.stop(timeout)
        for provider in self.providers:
            provider.shutdown()
