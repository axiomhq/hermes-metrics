"""Host stats for the containers Hermes runs commands in."""

from __future__ import annotations

import logging
import re
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from opentelemetry.metrics import CallbackOptions, Meter, Observation

HERMES_LABEL = "hermes-agent=1"
CACHE_SECONDS = 30.0
COMMAND_TIMEOUT = 5.0
STATS_FORMAT = "{{.ID}}\t{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.PIDs}}"

_UNITS = {
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}
_SIZE = re.compile(r"^\s*([0-9.]+)\s*([a-zA-Z]+)\s*$")

logger = logging.getLogger(__name__)


def parse_percent(text: str) -> float | None:
    try:
        return float(text.strip().rstrip("%"))
    except ValueError:
        return None


def parse_bytes(text: str) -> int | None:
    match = _SIZE.match(text.split("/")[0])
    if match is None:
        return None
    factor = _UNITS.get(match.group(2).lower())
    if factor is None:
        return None
    return int(float(match.group(1)) * factor)


@dataclass(frozen=True)
class Sample:
    name: str
    cpu_percent: float | None
    memory_bytes: int | None
    memory_percent: float | None
    pids: int | None


def run_docker(command: list[str]) -> str:
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=COMMAND_TIMEOUT, check=True
    )
    return result.stdout


def collect(runner: Callable[[list[str]], str]) -> list[Sample]:
    """Stat the containers Hermes labelled as its own."""
    try:
        ids = [
            line
            for line in runner(["docker", "ps", "-q", "--filter", f"label={HERMES_LABEL}"]).split()
            if line
        ]
        if not ids:
            return []
        raw = runner(["docker", "stats", "--no-stream", "--format", STATS_FORMAT, *ids])
    except Exception:
        logger.debug("container stats unavailable", exc_info=True)
        return []
    samples = []
    for line in raw.splitlines():
        fields = line.split("\t")
        if len(fields) < 6:
            continue
        _, name, cpu, memory, memory_percent, pids = fields[:6]
        samples.append(
            Sample(
                name=name.strip(),
                cpu_percent=parse_percent(cpu),
                memory_bytes=parse_bytes(memory),
                memory_percent=parse_percent(memory_percent),
                pids=int(pids) if pids.strip().isdigit() else None,
            )
        )
    return samples


class ContainerStats:
    """Publishes cpu, memory and process counts per Hermes container."""

    def __init__(
        self,
        meter: Meter,
        runner: Callable[[list[str]], str] = run_docker,
        cache_seconds: float = CACHE_SECONDS,
    ) -> None:
        self._runner = runner
        self._cache_seconds = cache_seconds
        self._samples: list[Sample] = []
        self._sampled_at = 0.0
        for name, unit, description, field in (
            ("cpu_percent", "%", "Container CPU use", "cpu_percent"),
            ("memory_bytes", "By", "Container memory use", "memory_bytes"),
            ("memory_percent", "%", "Container memory use against its limit", "memory_percent"),
            ("pids", "{process}", "Processes in the container", "pids"),
        ):
            meter.create_observable_gauge(
                f"hermes.container.{name}",
                callbacks=[self._sample_callback(field)],
                unit=unit,
                description=description,
            )
        meter.create_observable_gauge(
            "hermes.containers.running",
            callbacks=[self._running_callback],
            unit="{container}",
            description="Hermes containers currently running",
        )

    def forget(self) -> None:
        """Drop the cached sample so the next export shells out again."""
        self._sampled_at = 0.0

    def _sample(self) -> list[Sample]:
        now = time.monotonic()
        if now - self._sampled_at >= self._cache_seconds:
            self._samples = collect(self._runner)
            self._sampled_at = now
        return self._samples

    def _sample_callback(self, field: str) -> Any:
        def observe(options: CallbackOptions) -> Iterable[Observation]:
            return [
                Observation(getattr(sample, field), {"hermes.container": sample.name})
                for sample in self._sample()
                if getattr(sample, field) is not None
            ]

        observe.__name__ = f"hermess_metrics_container_{field}"
        return observe

    def _running_callback(self, options: CallbackOptions) -> Iterable[Observation]:
        return [Observation(len(self._sample()))]
