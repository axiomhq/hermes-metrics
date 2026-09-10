# SPDX-License-Identifier: Apache-2.0 OR MIT
"""Builds counters and histograms from hook payloads."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from opentelemetry.metrics import Meter

from .billing import billing_mode
from .events import Event

# Hermes reports api_duration in seconds despite documenting milliseconds.
_TOKEN_BUCKETS = (
    ("input_tokens", "input"),
    ("output_tokens", "output"),
    ("cache_read_tokens", "cache_read"),
    ("cache_write_tokens", "cache_write"),
    ("reasoning_tokens", "reasoning"),
)

logger = logging.getLogger(__name__)


def _model_label(payload: Mapping[str, Any]) -> str:
    response_model = payload.get("response_model")
    if isinstance(response_model, str) and response_model:
        return response_model
    model = payload.get("model")
    if isinstance(model, str) and model:
        return model.rsplit("/", 1)[-1]
    return "unknown"


def _elapsed_seconds(payload: Mapping[str, Any]) -> float:
    started_at = payload.get("started_at")
    ended_at = payload.get("ended_at")
    if isinstance(started_at, (int, float)) and isinstance(ended_at, (int, float)):
        return max(0.0, float(ended_at) - float(started_at))
    duration = payload.get("api_duration")
    return max(0.0, float(duration)) if isinstance(duration, (int, float)) else 0.0


def _text(payload: Mapping[str, Any], key: str, fallback: str = "unknown") -> str:
    value = payload.get(key)
    return value if isinstance(value, str) and value else fallback


class MetricRecorder:
    """Turns hook payloads into counters and histograms."""

    def __init__(self, meter: Meter) -> None:
        self._duration = meter.create_histogram(
            "gen_ai.client.operation.duration",
            unit="s",
            description="Elapsed time of a provider API call",
        )
        self._tokens = meter.create_counter(
            "gen_ai.client.token.usage",
            unit="{token}",
            description="Tokens consumed, split by bucket",
        )
        self._requests = meter.create_counter(
            "hermes.gen_ai.requests",
            unit="{request}",
            description="Provider API calls by outcome",
        )
        self._tool_duration = meter.create_histogram(
            "hermes.tool.duration",
            unit="s",
            description="Elapsed time of a tool execution",
        )
        self._tool_calls = meter.create_counter(
            "hermes.tool.calls",
            unit="{call}",
            description="Tool executions by outcome",
        )
        self._sessions = meter.create_counter(
            "hermes.sessions",
            unit="{session}",
            description="Sessions by outcome",
        )
        self._context = meter.create_histogram(
            "hermes.gen_ai.context_tokens",
            unit="{token}",
            description="Prompt size sent to the provider",
        )
        self._retry_depth = meter.create_histogram(
            "hermes.gen_ai.retry_depth",
            unit="{retry}",
            description="Retry attempt a provider call failed on",
        )
        self._exhausted = meter.create_counter(
            "hermes.gen_ai.retries_exhausted",
            unit="{request}",
            description="Provider calls that ran out of retries",
        )

    def handle(self, event: Any) -> None:
        if not isinstance(event, Event):
            return
        handler = {
            "api_request": self._api_request,
            "api_error": self._api_error,
            "tool_call": self._tool_call,
            "session_end": self._session_end,
        }.get(event.kind)
        if handler is not None:
            handler(event.payload)

    def _provider_dimensions(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "gen_ai.provider.name": _text(payload, "provider"),
            "gen_ai.request.model": _model_label(payload),
            "hermes.platform": _text(payload, "platform"),
            "hermes.billing_mode": billing_mode(
                _text(payload, "model", ""),
                _text(payload, "provider", ""),
                _text(payload, "base_url", ""),
            ),
        }

    def _api_request(self, payload: Mapping[str, Any]) -> None:
        dimensions = self._provider_dimensions(payload)
        self._duration.record(_elapsed_seconds(payload), dimensions)
        self._requests.add(
            1,
            {
                **dimensions,
                "hermes.outcome": "ok",
                "gen_ai.response.finish_reasons": _text(payload, "finish_reason"),
            },
        )
        usage = payload.get("usage")
        if not isinstance(usage, Mapping):
            return
        prompt_tokens = usage.get("prompt_tokens")
        if isinstance(prompt_tokens, int):
            self._context.record(prompt_tokens, dimensions)
        for source, label in _TOKEN_BUCKETS:
            count = usage.get(source)
            if isinstance(count, int) and count:
                self._tokens.add(count, {**dimensions, "gen_ai.token.type": label})

    def _api_error(self, payload: Mapping[str, Any]) -> None:
        dimensions = self._provider_dimensions(payload)
        self._duration.record(_elapsed_seconds(payload), dimensions)
        attributes: dict[str, Any] = {
            **dimensions,
            "hermes.outcome": "error",
            "error.type": _text(payload, "reason"),
        }
        status_code = payload.get("status_code")
        if isinstance(status_code, int):
            attributes["http.response.status_code"] = status_code
        self._requests.add(1, attributes)
        retry_count = payload.get("retry_count")
        if isinstance(retry_count, int):
            self._retry_depth.record(retry_count, dimensions)
            max_retries = payload.get("max_retries")
            if isinstance(max_retries, int) and retry_count >= max_retries:
                self._exhausted.add(1, {**dimensions, "error.type": _text(payload, "reason")})

    def _tool_call(self, payload: Mapping[str, Any]) -> None:
        duration_ms = payload.get("duration_ms")
        elapsed = float(duration_ms) / 1000.0 if isinstance(duration_ms, (int, float)) else 0.0
        dimensions = {
            "gen_ai.tool.name": _text(payload, "tool_name"),
            "hermes.platform": _text(payload, "platform"),
        }
        succeeded = str(payload.get("status") or "").lower() in ("ok", "success", "")
        self._tool_duration.record(elapsed, dimensions)
        attributes: dict[str, Any] = {
            **dimensions,
            "hermes.outcome": "ok" if succeeded else "error",
        }
        if not succeeded:
            attributes["error.type"] = _text(payload, "error_type")
        self._tool_calls.add(1, attributes)

    def _session_end(self, payload: Mapping[str, Any]) -> None:
        outcome = "interrupted" if payload.get("interrupted") else "completed"
        self._sessions.add(
            1,
            {"hermes.outcome": outcome, "hermes.platform": _text(payload, "platform")},
        )
