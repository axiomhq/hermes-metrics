"""Content policy for everything leaving the machine.

Hermes hook payloads carry prompts, conversation history, tool arguments and
tool results. The level chooses how much of that content ships. Credential
masking and size caps are unconditional, so raising the level widens what is
observable without ever widening what is leaked.
"""

from __future__ import annotations

from dataclasses import dataclass

LEVEL_METADATA = "metadata"
LEVEL_TOOLS = "tools"
LEVEL_FULL = "full"

# Ordered from least to most revealing; the first entry is the fallback.
LEVELS = (LEVEL_METADATA, LEVEL_TOOLS, LEVEL_FULL)
DEFAULT_LEVEL = LEVEL_METADATA

CLASS_METADATA = "metadata"
CLASS_TOOL_IO = "tool_io"
CLASS_MESSAGES = "messages"

DEFAULT_MAX_CHARS = 12000
MAX_DEPTH = 8
MAX_SEQUENCE = 200

MASK = "[redacted]"
_ELLIPSIS = "…"
_TOO_DEEP = "[depth limit]"

_ALLOWED_BY_LEVEL = {
    LEVEL_METADATA: frozenset({CLASS_METADATA}),
    LEVEL_TOOLS: frozenset({CLASS_METADATA, CLASS_TOOL_IO}),
    LEVEL_FULL: frozenset({CLASS_METADATA, CLASS_TOOL_IO, CLASS_MESSAGES}),
}

_SENSITIVE_EXACT = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "cookie",
        "credentials",
        "id_token",
        "passwd",
        "password",
        "private_key",
        "proxy_authorization",
        "refresh_token",
        "secret",
        "session_key",
        "set_cookie",
        "token",
    }
)
_SENSITIVE_SUFFIXES = ("_api_key", "_secret", "_password", "_passwd", "_credential")


def is_sensitive_key(key: object) -> bool:
    """Whether a mapping key names a credential rather than describing data."""
    if not isinstance(key, str):
        return False
    normalised = key.lower().replace("-", "_")
    return normalised in _SENSITIVE_EXACT or normalised.endswith(_SENSITIVE_SUFFIXES)


@dataclass(frozen=True)
class Redactor:
    """Applies one content level plus the unconditional protections."""

    level: str = DEFAULT_LEVEL
    max_chars: int = DEFAULT_MAX_CHARS

    @classmethod
    def for_level(cls, level: str, max_chars: int = DEFAULT_MAX_CHARS) -> Redactor:
        """Resolve a level name, falling back to the most conservative one."""
        candidate = level.strip().lower()
        resolved = candidate if candidate in _ALLOWED_BY_LEVEL else DEFAULT_LEVEL
        return cls(level=resolved, max_chars=max(1, max_chars))

    def allows(self, content_class: str) -> bool:
        return content_class in _ALLOWED_BY_LEVEL[self.level]

    def text(self, value: object, content_class: str) -> str | None:
        """A capped string, or ``None`` when the level withholds this class."""
        if not self.allows(content_class):
            return None
        return self._cap(value if isinstance(value, str) else repr(value))

    def structure(self, value: object, content_class: str) -> object | None:
        """A scrubbed copy, or ``None`` when the level withholds this class."""
        if not self.allows(content_class):
            return None
        return self._scrub(value, depth=0)

    def _cap(self, value: str) -> str:
        if len(value) <= self.max_chars:
            return value
        return value[: self.max_chars - 1] + _ELLIPSIS

    def _scrub(self, value: object, *, depth: int) -> object:
        if depth > MAX_DEPTH:
            return _TOO_DEEP
        if isinstance(value, str):
            return self._cap(value)
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, dict):
            return {
                str(key): MASK if is_sensitive_key(key) else self._scrub(item, depth=depth + 1)
                for key, item in list(value.items())[:MAX_SEQUENCE]
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self._scrub(item, depth=depth + 1) for item in list(value)[:MAX_SEQUENCE]]
        return self._cap(repr(value))
