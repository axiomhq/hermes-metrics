# SPDX-License-Identifier: Apache-2.0 OR MIT
"""The monitors setup creates alongside the datasets."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .control_plane import AxiomError

THRESHOLD = "Threshold"
ABOVE = "Above"

# Starting points to tune, not claims about any workload.
TOKEN_BUDGET_PER_15M = 500_000
SPEND_BUDGET_PER_HOUR = 5.0
MAX_SUBAGENTS = 8


@dataclass(frozen=True)
class Budgets:
    """The three thresholds that depend on the workload rather than the plugin."""

    tokens_per_15m: float = TOKEN_BUDGET_PER_15M
    spend_per_hour: float = SPEND_BUDGET_PER_HOUR
    max_subagents: float = MAX_SUBAGENTS


def _q(dataset: str, metric: str) -> str:
    """MPL escapes identifiers with backticks, which these names need."""
    return f"`{dataset}`:`{metric}`"


def pack(datasets: dict[str, str], budgets: Budgets | None = None) -> list[dict[str, Any]]:
    """Monitor definitions for the metrics dataset setup just created."""
    metrics = datasets.get("metrics", "")
    return _alerts(metrics, budgets or Budgets()) if metrics else []


def _alerts(dataset: str, budgets: Budgets) -> list[dict[str, Any]]:
    return [
        {
            "name": "Hermes telemetry is being dropped",
            "description": "The plugin's queue overflowed, so every other number is understated.",
            "mplQuery": f"{_q(dataset, 'hermes.telemetry.dropped')} "
            "| align to 5m using max | group using sum",
            "threshold": 0,
            "intervalMinutes": 5,
            "rangeMinutes": 15,
        },
        {
            "name": "Hermes stopped reporting",
            "description": "No telemetry arrived, so Hermes or the plugin is down.",
            "mplQuery": f"{_q(dataset, 'hermes.telemetry.accepted')} "
            "| align to 5m using max | group using sum",
            "threshold": 0,
            "alertOnNoData": True,
            "intervalMinutes": 10,
            "rangeMinutes": 30,
        },
        {
            "name": "Hermes provider calls are failing",
            "description": "Provider calls returned errors in the window.",
            "mplQuery": f"{_q(dataset, 'hermes.gen_ai.requests')} "
            '| where `hermes.outcome` == "error" | align to 10m using max | group using sum',
            "threshold": 0,
            "intervalMinutes": 10,
            "rangeMinutes": 30,
        },
        {
            "name": "Hermes tool calls are failing",
            "description": "Tool executions returned errors in the window.",
            "mplQuery": f"{_q(dataset, 'hermes.tool.calls')} "
            '| where `hermes.outcome` == "error" | align to 10m using max | group using sum',
            "threshold": 0,
            "intervalMinutes": 10,
            "rangeMinutes": 30,
        },
        {
            "name": "Hermes token use is high",
            "description": "Tokens per 15 minutes passed the budget, currently "
            f"{budgets.tokens_per_15m:,.0f}.",
            "mplQuery": f"{_q(dataset, 'gen_ai.client.token.usage')} "
            "| map rate | map * 900 | align to 15m using avg | group using sum",
            "threshold": budgets.tokens_per_15m,
            "intervalMinutes": 15,
            "rangeMinutes": 15,
        },
        {
            "name": "Hermes spend is high",
            "description": "Spend per hour passed the budget, currently "
            f"${budgets.spend_per_hour:,.2f}.",
            "mplQuery": f"{_q(dataset, 'hermes.gen_ai.cost')} "
            "| map rate | map * 3600 | align to 1h using avg | group using sum",
            "threshold": budgets.spend_per_hour,
            "intervalMinutes": 30,
            "rangeMinutes": 60,
        },
        {
            "name": "Hermes subagent fan-out is high",
            "description": f"More than {budgets.max_subagents:,.0f} subagents ran at once.",
            "mplQuery": f"{_q(dataset, 'hermes.subagents.active')} "
            "| align to 1m using max | group using max",
            "threshold": budgets.max_subagents,
            "intervalMinutes": 5,
            "rangeMinutes": 10,
        },
    ]


@dataclass(frozen=True)
class Report:
    """What creating the pack achieved."""

    created: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.created) + len(self.failed)


def create(plane: Any, datasets: dict[str, str], budgets: Budgets | None = None) -> Report:
    """Create every monitor in the pack, collecting what each one did."""
    report = Report()
    for spec in pack(datasets, budgets):
        body = dict(spec, type=THRESHOLD, operator=ABOVE, notifierIds=[])
        try:
            plane.create_monitor(body)
        except AxiomError as exc:
            report.failed.append((str(spec["name"]), exc.message))
        else:
            report.created.append(str(spec["name"]))
    return report
