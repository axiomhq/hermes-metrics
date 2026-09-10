"""Log records built from Hermes observer hooks.

Records carry the Hermes identifiers, so a record found in the logs dataset
names the session, turn and request whose span holds the rest of the story.
Raw provider and tool error text is content and is governed by the redactor;
the error class, status code and identifiers are metadata and always present.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.sdk._logs import Logger

from .events import Event
from .redaction import CLASS_MESSAGES, CLASS_TOOL_IO, Redactor

_IDENTIFIERS = (
    ("hermes.session_id", "session_id"),
    ("hermes.turn_id", "turn_id"),
    ("hermes.api_request_id", "api_request_id"),
    ("hermes.tool_call_id", "tool_call_id"),
)

logger = logging.getLogger(__name__)


def _identifiers(payload: Mapping[str, Any]) -> dict[str, Any]:
    found = {}
    for key, source in _IDENTIFIERS:
        value = payload.get(source)
        if isinstance(value, str) and value:
            found[key] = value
    return found


class LogRecorder:
    """Turns hook payloads into log records."""

    def __init__(self, logger: Logger, redactor: Redactor) -> None:
        self._logger = logger
        self._redactor = redactor

    def handle(self, event: Any) -> None:
        if not isinstance(event, Event):
            return
        handler = {
            "api_error": self._api_error,
            "tool_call": self._tool_call,
            "diagnostic": self._diagnostic,
        }.get(event.kind)
        if handler is not None:
            handler(event)

    def _emit(
        self,
        event: Event,
        body: str,
        severity: SeverityNumber,
        attributes: Mapping[str, Any],
    ) -> None:
        self._logger.emit(
            LogRecord(
                timestamp=int(event.observed_at * 1_000_000_000),
                observed_timestamp=int(event.observed_at * 1_000_000_000),
                severity_number=severity,
                severity_text=severity.name,
                body=body,
                attributes=dict(attributes),
            )
        )

    def _api_error(self, event: Event) -> None:
        payload = event.payload
        reason = payload.get("reason")
        attributes: dict[str, Any] = {
            **_identifiers(payload),
            "error.type": str(reason) if reason else "unknown",
        }
        for key, source in (
            ("http.response.status_code", "status_code"),
            ("hermes.retry_count", "retry_count"),
            ("hermes.retryable", "retryable"),
            ("gen_ai.provider.name", "provider"),
        ):
            value = payload.get(source)
            if isinstance(value, (int, bool, str)) and value != "":
                attributes[key] = value
        text = self._redactor.text(payload.get("error"), CLASS_MESSAGES)
        if text is not None:
            attributes["exception.message"] = text
        severity = SeverityNumber.WARN if payload.get("retryable") else SeverityNumber.ERROR
        self._emit(event, "provider call failed", severity, attributes)

    def _tool_call(self, event: Event) -> None:
        payload = event.payload
        if str(payload.get("status") or "").lower() in ("ok", "success", ""):
            return
        attributes: dict[str, Any] = {
            **_identifiers(payload),
            "gen_ai.tool.name": str(payload.get("tool_name") or "unknown"),
            "error.type": str(payload.get("error_type") or "unknown"),
        }
        text = self._redactor.text(payload.get("error_message"), CLASS_TOOL_IO)
        if text is not None:
            attributes["exception.message"] = text
        self._emit(event, "tool execution failed", SeverityNumber.ERROR, attributes)

    def _diagnostic(self, event: Event) -> None:
        payload = dict(event.payload)
        message = str(payload.pop("message", ""))
        self._emit(event, message, SeverityNumber.WARN, payload)
