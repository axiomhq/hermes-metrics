# SPDX-License-Identifier: Apache-2.0 OR MIT
"""Builds spans from hook payloads in Axiom's generative AI conventions."""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from opentelemetry import trace as trace_api
from opentelemetry.context import Context
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from .billing import billing_mode
from .events import Event
from .redaction import CLASS_MESSAGES, CLASS_TOOL_IO, Redactor

SCHEMA_URL = "https://axiom.co/ai/schemas/0.0.2"
SDK_NAME = "hermes-metrics"
AGENT_NAME = "hermes"
DEFAULT_MAX_LIVE = 256

_USAGE_ATTRIBUTES = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)

logger = logging.getLogger(__name__)


def _model_label(payload: Mapping[str, Any]) -> str:
    response_model = payload.get("response_model")
    if isinstance(response_model, str) and response_model:
        return response_model
    model = payload.get("model")
    if isinstance(model, str) and model:
        return model.rsplit("/", 1)[-1]
    return ""


def _nanos(seconds: float) -> int:
    return int(seconds * 1_000_000_000)


class TraceRecorder:
    """Turns hook payloads into spans; live state is worker-owned, so unlocked."""

    def __init__(
        self,
        tracer: trace_api.Tracer,
        redactor: Redactor,
        max_live: int = DEFAULT_MAX_LIVE,
    ) -> None:
        self._tracer = tracer
        self._redactor = redactor
        self._max_live = max(1, max_live)
        self._sessions: OrderedDict[str, Span] = OrderedDict()
        self._turns: OrderedDict[str, Span] = OrderedDict()

    @property
    def max_live(self) -> int:
        return self._max_live

    @property
    def live_session_count(self) -> int:
        return len(self._sessions)

    def handle(self, event: Any) -> None:
        if not isinstance(event, Event):
            return
        handler = {
            "session_start": self._session_start,
            "session_end": self._session_end,
            "turn_start": self._turn_start,
            "turn_end": self._turn_end,
            "api_request": self._api_request,
            "api_error": self._api_error,
            "tool_call": self._tool_call,
        }.get(event.kind)
        if handler is not None:
            handler(event)

    def _common(self, payload: Mapping[str, Any], step: str) -> dict[str, Any]:
        platform = payload.get("platform")
        return {
            "gen_ai.capability.name": platform
            if isinstance(platform, str) and platform
            else AGENT_NAME,
            "gen_ai.step.name": step,
            "axiom.gen_ai.schema_url": SCHEMA_URL,
            "axiom.gen_ai.sdk.name": SDK_NAME,
        }

    def _remember(self, store: OrderedDict[str, Span], key: str, span: Span) -> None:
        store[key] = span
        while len(store) > self._max_live:
            _, evicted = store.popitem(last=False)
            evicted.end()

    def _parent_context(self, span: Span | None) -> Context:
        return trace_api.set_span_in_context(span) if span is not None else Context()

    def _session_start(self, event: Event) -> None:
        payload = event.payload
        session_id = str(payload.get("session_id") or "")
        span = self._tracer.start_span(
            f"invoke_agent {AGENT_NAME}",
            context=Context(),
            kind=SpanKind.CLIENT,
            attributes={
                **self._common(payload, "invoke_agent"),
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": AGENT_NAME,
                "gen_ai.conversation.id": session_id,
            },
            start_time=_nanos(event.observed_at),
        )
        self._remember(self._sessions, session_id, span)

    def _session_end(self, event: Event) -> None:
        payload = event.payload
        span = self._sessions.pop(str(payload.get("session_id") or ""), None)
        if span is None:
            return
        if payload.get("interrupted"):
            span.set_attribute("hermes.interrupted", True)
        span.end(end_time=_nanos(event.observed_at))

    def _turn_start(self, event: Event) -> None:
        payload = event.payload
        turn_id = str(payload.get("turn_id") or "")
        parent = self._sessions.get(str(payload.get("session_id") or ""))
        span = self._tracer.start_span(
            "turn",
            context=self._parent_context(parent),
            kind=SpanKind.INTERNAL,
            attributes={
                **self._common(payload, "turn"),
                "gen_ai.conversation.id": str(payload.get("session_id") or ""),
                "hermes.turn_id": turn_id,
            },
            start_time=_nanos(event.observed_at),
        )
        self._remember(self._turns, turn_id, span)

    def _turn_end(self, event: Event) -> None:
        payload = event.payload
        span = self._turns.pop(str(payload.get("turn_id") or ""), None)
        if span is not None:
            span.end(end_time=_nanos(event.observed_at))

    def _start_chat_span(self, payload: Mapping[str, Any]) -> Span:
        label = _model_label(payload)
        parent = self._turns.get(str(payload.get("turn_id") or "")) or self._sessions.get(
            str(payload.get("session_id") or "")
        )
        attributes: dict[str, Any] = {
            **self._common(payload, "chat"),
            "gen_ai.operation.name": "chat",
            "gen_ai.conversation.id": str(payload.get("session_id") or ""),
            "hermes.billing_mode": billing_mode(
                str(payload.get("model") or ""),
                str(payload.get("provider") or ""),
                str(payload.get("base_url") or ""),
            ),
        }
        for key, source in (
            ("gen_ai.provider.name", "provider"),
            ("gen_ai.request.model", "model"),
            ("gen_ai.response.model", "response_model"),
            ("gen_ai.response.id", "api_request_id"),
        ):
            value = payload.get(source)
            if isinstance(value, str) and value:
                attributes[key] = value
        started_at = payload.get("started_at")
        return self._tracer.start_span(
            f"chat {label}".strip(),
            context=self._parent_context(parent),
            kind=SpanKind.CLIENT,
            attributes=attributes,
            start_time=_nanos(started_at) if isinstance(started_at, (int, float)) else None,
        )

    def _api_request(self, event: Event) -> None:
        payload = event.payload
        span = self._start_chat_span(payload)
        finish_reason = payload.get("finish_reason")
        if isinstance(finish_reason, str) and finish_reason:
            span.set_attribute("gen_ai.response.finish_reasons", (finish_reason,))
        usage = payload.get("usage")
        if isinstance(usage, Mapping):
            for name in _USAGE_ATTRIBUTES:
                value = usage.get(name)
                if isinstance(value, int):
                    span.set_attribute(f"gen_ai.usage.{name}", value)
        output = self._redactor.text(payload.get("assistant_message"), CLASS_MESSAGES)
        if output is not None:
            span.set_attribute("gen_ai.output.messages", output)
        ended_at = payload.get("ended_at")
        span.end(end_time=_nanos(ended_at) if isinstance(ended_at, (int, float)) else None)

    def _api_error(self, event: Event) -> None:
        payload = event.payload
        span = self._start_chat_span(payload)
        reason = payload.get("reason")
        span.set_attribute("error.type", str(reason) if reason else "unknown")
        for key, source in (
            ("http.response.status_code", "status_code"),
            ("hermes.retry_count", "retry_count"),
            ("hermes.retryable", "retryable"),
        ):
            value = payload.get(source)
            if isinstance(value, (int, bool)):
                span.set_attribute(key, value)
        span.set_status(Status(StatusCode.ERROR, str(payload.get("error") or reason or "")))
        ended_at = payload.get("ended_at")
        span.end(end_time=_nanos(ended_at) if isinstance(ended_at, (int, float)) else None)

    def _tool_call(self, event: Event) -> None:
        payload = event.payload
        tool_name = str(payload.get("tool_name") or "")
        duration_ms = payload.get("duration_ms")
        elapsed = float(duration_ms) / 1000.0 if isinstance(duration_ms, (int, float)) else 0.0
        ended_at = event.observed_at
        parent = self._turns.get(str(payload.get("turn_id") or "")) or self._sessions.get(
            str(payload.get("session_id") or "")
        )
        attributes: dict[str, Any] = {
            **self._common(payload, "execute_tool"),
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": tool_name,
            "gen_ai.conversation.id": str(payload.get("session_id") or ""),
        }
        call_id = payload.get("tool_call_id")
        if isinstance(call_id, str) and call_id:
            attributes["gen_ai.tool.call.id"] = call_id
        arguments = self._redactor.structure(payload.get("args"), CLASS_TOOL_IO)
        if arguments is not None:
            attributes["gen_ai.tool.arguments"] = repr(arguments)
        span = self._tracer.start_span(
            f"execute_tool {tool_name}".strip(),
            context=self._parent_context(parent),
            kind=SpanKind.INTERNAL,
            attributes=attributes,
            start_time=_nanos(ended_at - elapsed),
        )
        result = self._redactor.text(payload.get("result"), CLASS_TOOL_IO)
        if result is not None:
            span.set_attribute("gen_ai.tool.message", result)
        if str(payload.get("status") or "").lower() not in ("ok", "success", ""):
            span.set_attribute("error.type", str(payload.get("error_type") or "unknown"))
            span.set_status(Status(StatusCode.ERROR, str(payload.get("error_message") or "")))
        span.end(end_time=_nanos(ended_at))
