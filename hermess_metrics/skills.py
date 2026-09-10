"""Publishes the installed skill list and per-skill load counts."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from opentelemetry.metrics import CallbackOptions, Meter, Observation

from .events import KIND_TOOL_CALL, Event

SKILL_TOOL = "skill_view"
MANIFEST = "SKILL.md"
MAX_TRACKED = 512
CACHE_SECONDS = 60.0
_FRONTMATTER_BYTES = 4000

logger = logging.getLogger(__name__)


def _skill_name(manifest: Path) -> str:
    try:
        from agent.skill_utils import parse_frontmatter

        head = manifest.read_text(encoding="utf-8", errors="replace")[:_FRONTMATTER_BYTES]
        frontmatter, _ = parse_frontmatter(head)
        name = frontmatter.get("name") if isinstance(frontmatter, Mapping) else None
    except Exception:
        name = None
    return str(name) if name else manifest.parent.name


def scan_skills(roots: Iterable[Path], disabled: set[str]) -> dict[str, bool]:
    """Skill name to whether it is enabled, walking each root for manifests."""
    found: dict[str, bool] = {}
    for root in roots:
        try:
            manifests = sorted(Path(root).rglob(MANIFEST))
        except OSError:
            continue
        for manifest in manifests:
            if _excluded(manifest):
                continue
            name = _skill_name(manifest)
            found[name] = name not in disabled
    return found


def _excluded(manifest: Path) -> bool:
    try:
        from agent.skill_utils import is_excluded_skill_path

        return bool(is_excluded_skill_path(manifest))
    except Exception:
        return False


def installed_skills() -> dict[str, bool]:
    """Skills as Hermes currently has them on disk."""
    try:
        from agent.skill_utils import get_all_skills_dirs, get_disabled_skill_names

        return scan_skills(
            [Path(d) for d in get_all_skills_dirs()], set(get_disabled_skill_names())
        )
    except Exception:
        logger.debug("skill directories unavailable", exc_info=True)
        return {}


class SkillInventory:
    """Publishes the skill list and a load count seeded from it."""

    def __init__(
        self,
        meter: Meter,
        source: Callable[[], Mapping[str, bool]] = installed_skills,
        max_tracked: int = MAX_TRACKED,
        cache_seconds: float = CACHE_SECONDS,
    ) -> None:
        self._source = source
        self._max_tracked = max(1, max_tracked)
        self._cache_seconds = cache_seconds
        self._counts: dict[str, int] = {}
        self._cached: dict[str, bool] = {}
        self._cached_at = 0.0
        meter.create_observable_gauge(
            "hermes.skills.installed",
            callbacks=[self._installed_callback],
            unit="{skill}",
            description="Skills present on disk, marked enabled or disabled",
        )
        meter.create_observable_counter(
            "hermes.skill.invocations",
            callbacks=[self._invocations_callback],
            unit="{load}",
            description="Skill loads, zero for an installed skill never loaded",
        )

    def forget(self) -> None:
        """Drop the cached scan so the next export walks the directories."""
        self._cached_at = 0.0

    def handle(self, event: Any) -> None:
        if not isinstance(event, Event) or event.kind != KIND_TOOL_CALL:
            return
        if str(event.payload.get("tool_name") or "") != SKILL_TOOL:
            return
        args = event.payload.get("args")
        if not isinstance(args, Mapping):
            return
        name = args.get("name")
        if not isinstance(name, str) or not name:
            return
        self._counts[name] = self._counts.get(name, 0) + 1

    def _scan(self) -> dict[str, bool]:
        now = time.monotonic()
        if now - self._cached_at >= self._cache_seconds:
            try:
                self._cached = dict(self._source())
            except Exception:
                logger.debug("skill scan failed", exc_info=True)
                self._cached = {}
            self._cached_at = now
        return self._cached

    def _tracked(self) -> dict[str, bool]:
        merged = dict(self._scan())
        for name in self._counts:
            merged.setdefault(name, True)
        return dict(sorted(merged.items())[: self._max_tracked])

    def _observe(self, value_of: Callable[[str], int]) -> Iterable[Observation]:
        return [
            Observation(value_of(name), {"hermes.skill": name, "hermes.enabled": enabled})
            for name, enabled in self._tracked().items()
        ]

    def _installed_callback(self, options: CallbackOptions) -> Iterable[Observation]:
        return self._observe(lambda _: 1)

    def _invocations_callback(self, options: CallbackOptions) -> Iterable[Observation]:
        return self._observe(lambda name: self._counts.get(name, 0))
