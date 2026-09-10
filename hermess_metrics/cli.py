# SPDX-License-Identifier: Apache-2.0 OR MIT
"""The hermes axiom subcommand: set up telemetry and report its state."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import alerts
from .config import Config
from .control_plane import (
    DEFAULT_DOMAIN,
    PERMISSION_DATASETS,
    PERMISSION_TOKENS,
    AxiomError,
    ControlPlane,
)
from .provision import DEFAULT_PREFIX, Provisioned, env_values, provision, write_env

COMMAND = "axiom"
ENV_FILENAME = ".env"

CHOICE_PROVISION = "1"
CHOICE_ADOPT = "2"
QUESTION = """Where should Hermes send its telemetry?

  1) Provision a new Axiom org now. Free, ready in seconds, and deleted
     within a day unless you follow the claim link to keep it.
  2) Use an Axiom org you already have. The token needs datasets:create.
     Add apiTokens:create as well and setup will mint a narrow token to
     store instead of yours; without it your token is stored as-is.

Choose 1 or 2: """
ORG_QUESTION = "Axiom org id (find it in the console URL; press enter if the token is org-scoped): "

logger = logging.getLogger(__name__)


def hermes_home() -> Path:
    return Path.home() / ".hermes"


def build_parser(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="axiom_action", required=True)
    setup = actions.add_parser("setup", help="Create datasets and a token, then save them")
    setup.add_argument(
        "--token",
        help=f"API token for an org you already have; needs {PERMISSION_DATASETS}, "
        f"optionally {PERMISSION_TOKENS} to mint a narrower one",
    )
    setup.add_argument("--org", default="", help="Org id, if your token is not org-scoped")
    setup.add_argument("--provision", action="store_true", help="Provision a new temporary org")
    setup.add_argument("--prefix", default=DEFAULT_PREFIX, help="Dataset name prefix")
    setup.add_argument("--domain", default=DEFAULT_DOMAIN, help="Axiom API host")
    setup.add_argument("--region", help="Edge deployment for a new org")
    setup.add_argument("--env-file", help="Where to write settings")
    actions.add_parser("status", help="Show what the plugin is configured to do")
    alerts_cmd = actions.add_parser("alerts", help="Create the monitor pack in your org")
    alerts_cmd.add_argument("--token", help="API token that can create monitors")
    alerts_cmd.add_argument("--org", default="", help="Org id, if your token is not org-scoped")
    alerts_cmd.add_argument("--domain", default=DEFAULT_DOMAIN, help="Axiom API host")
    alerts_cmd.add_argument("--tokens-per-15m", type=float, help="Token budget per 15 minutes")
    alerts_cmd.add_argument("--spend-per-hour", type=float, help="Spend budget per hour, USD")
    alerts_cmd.add_argument("--max-subagents", type=float, help="Concurrent subagents allowed")
    alerts_cmd.add_argument(
        "--defaults", action="store_true", help="Take every default without asking"
    )


def register_cli(ctx: Any) -> None:
    ctx.register_cli_command(
        COMMAND,
        "Set up and inspect Axiom telemetry",
        build_parser,
        handle,
        description="Provision or adopt an Axiom org and wire Hermes telemetry to it.",
    )


def handle(args: argparse.Namespace) -> int:
    action = getattr(args, "axiom_action", None)
    if action == "status":
        return run_status()
    if action == "alerts":
        return run_alerts(args)
    return run_setup(args)


BUDGET_PROMPTS = (
    ("tokens_per_15m", "tokens_per_15m", "Tokens per 15 minutes before alerting", "{:,.0f}"),
    ("spend_per_hour", "spend_per_hour", "Spend per hour before alerting, USD", "{:,.2f}"),
    ("max_subagents", "max_subagents", "Concurrent subagents before alerting", "{:,.0f}"),
)


def _number(raw: str, fallback: float) -> float:
    """Accept what a person types: blank, commas, a currency sign."""
    cleaned = raw.strip().lstrip("$").replace(",", "").replace("_", "")
    if not cleaned:
        return fallback
    try:
        value = float(cleaned)
    except ValueError as exc:
        raise ValueError(f"{raw.strip()!r} is not a number") from exc
    if value <= 0:
        raise ValueError("a budget has to be greater than zero")
    return value


def _budgets(ask: Callable[[str], str], args: argparse.Namespace) -> alerts.Budgets:
    """Take budgets from flags, then from the operator, then from the defaults."""
    chosen: dict[str, float] = {}
    asked = False
    for field_name, flag, question, fmt in BUDGET_PROMPTS:
        given = getattr(args, flag, None)
        default = float(getattr(alerts.Budgets(), field_name))
        if given is not None:
            chosen[field_name] = float(given)
            continue
        if getattr(args, "defaults", False):
            chosen[field_name] = default
            continue
        if not asked:
            asked = True
        chosen[field_name] = _number(ask(f"{question} [{fmt.format(default)}]: "), default)
    return alerts.Budgets(**chosen)


def run_alerts(
    args: argparse.Namespace,
    ask: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
) -> int:
    """Create the monitor pack against the datasets already configured."""
    config = Config.from_env()
    if not config.configured_signals:
        out("Nothing to alert on yet; run `hermes axiom setup` first.")
        return 1
    try:
        budgets = _budgets(ask, args)
    except (ValueError, EOFError, KeyboardInterrupt) as exc:
        out(f"Alerts cancelled: {exc}")
        return 2
    token = str(getattr(args, "token", "") or config.token)
    plane = ControlPlane(domain=args.domain, token=token, org=str(args.org or config.org))
    datasets = {signal: config.dataset_for(signal) for signal in config.configured_signals}
    report = alerts.create(plane, datasets, budgets)
    _report_alerts(report, out)
    return 0 if report.created else 1


def _report_alerts(report: alerts.Report, out: Callable[[str], None]) -> None:
    out(f"Created {len(report.created)} of {report.total} monitors.")
    for name in report.created:
        out(f"  ok      {name}")
    for name, reason in report.failed:
        out(f"  skipped {name}")
        out(f"          {reason[:110]}")
    if report.created:
        out("")
        out("Every threshold can be changed later in the Axiom console.")


def _choose(ask: Callable[[str], str], args: argparse.Namespace) -> tuple[str | None, str]:
    """Return the org token to adopt and its org id, or None to provision."""
    if args.token:
        return str(args.token), str(args.org or "")
    if args.provision:
        return None, ""
    answer = ask(QUESTION).strip()
    if answer == CHOICE_PROVISION:
        return None, ""
    if answer == CHOICE_ADOPT:
        token = ask("Axiom API token: ").strip()
        if not token:
            raise ValueError("no token given")
        org = args.org or ask(ORG_QUESTION).strip()
        return token, str(org)
    raise ValueError(f"expected {CHOICE_PROVISION} or {CHOICE_ADOPT}, got {answer!r}")


def run_setup(
    args: argparse.Namespace,
    ask: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
) -> int:
    try:
        org_token, org = _choose(ask, args)
    except (ValueError, EOFError, KeyboardInterrupt) as exc:
        out(f"Setup cancelled: {exc}")
        return 2

    plane = ControlPlane(domain=args.domain, token=org_token or "", org=org)
    try:
        result = provision(plane, prefix=args.prefix, region=args.region, org_token=org_token)
    except AxiomError as exc:
        accepted = plane.token_is_accepted() if exc.status in (401, 403) else None
        for line in _explain(exc, org_token, org, accepted):
            out(line)
        return 1

    target = Path(args.env_file) if args.env_file else hermes_home() / ENV_FILENAME
    write_env(target, env_values(result))
    _report(result, target, out)
    return 0


TOKEN_SETTINGS_URL = "https://app.axiom.co/settings/api-tokens"


def _explain_denied(exc: AxiomError, org: str, accepted: bool | None) -> list[str]:
    """Report what was refused, and what a probe showed, without guessing past it."""
    needed = exc.permission or PERMISSION_DATASETS
    lines = [f"Setup failed: {exc.operation or 'the request'} was refused ({exc.status})."]
    if exc.message and exc.message != "forbidden":
        lines.append(f"  Axiom said: {exc.message}")
    if exc.trace_id:
        lines.append(f"  Axiom trace id: {exc.trace_id}")
    lines.append("")
    lines.append(f"That call needs the {needed} permission.")
    if accepted is True:
        lines.append("Other reads with the same token succeeded, so the token is accepted")
        lines.append(f"for this org and {needed} is the permission it is missing.")
        lines.append(f"Add it at {TOKEN_SETTINGS_URL}")
        return lines
    if accepted is False:
        lines.append("Every other read with the same token was refused too, so it may be")
        lines.append("missing more than this one permission, or not be accepted for this")
        lines.append(f"org. Check its permissions at {TOKEN_SETTINGS_URL}")
        if org:
            lines.append(f"and that it belongs to org {org!r}.")
        else:
            lines.append("and pass --org <org-id> if it is not org-scoped.")
        return lines
    lines.append(f"Check the token's permissions at {TOKEN_SETTINGS_URL}")
    return lines


def _explain(
    exc: AxiomError, org_token: str | None, org: str = "", accepted: bool | None = None
) -> list[str]:
    """Turn an API failure into something the operator can act on."""
    if exc.status in (401, 403):
        return _explain_denied(exc, org, accepted)
    if exc.status != 429 or org_token:
        return [f"Setup failed: {exc}"]
    lines = ["Setup failed: Axiom is rate limiting new orgs from this address."]
    if exc.seconds_until_reset:
        when = datetime.fromtimestamp(exc.resets_at or 0, tz=UTC)
        hours = exc.seconds_until_reset / 3600
        lines.append(f"  The limit resets at {when:%Y-%m-%d %H:%M} UTC, in about {hours:.0f}h.")
    lines.append("  To set up now, use an Axiom org you already have:")
    lines.append("    hermes axiom setup --token <your-api-token>")
    return lines


def _report(result: Provisioned, target: Path, out: Callable[[str], None]) -> None:
    out("Axiom telemetry is configured.")
    for signal, dataset in result.datasets.items():
        out(f"  {signal:<8} {dataset}")
    out(f"  settings {target}")
    if not result.minted:
        out("")
        out("Stored the token you supplied, because minting a narrower one was")
        out(f"refused. Add {PERMISSION_TOKENS} to that token and re-run to store a")
        out("token limited to writing these three datasets instead.")
    elif not result.can_query:
        out("")
        out("The saved token can write telemetry but not read it back, because the")
        out("token you supplied cannot grant query access on these datasets.")
    if result.needs_claim:
        out("")
        out("Claim the org to keep this data and let alerts fire:")
        out(f"  {result.claim_url}")
        out(f"It is deleted after {result.expires_at} if nobody does.")


def run_status(out: Callable[[str], None] = print) -> int:
    from . import transport

    config = Config.from_env()
    hint = transport.sdk_hint()
    if hint is not None:
        out("Axiom telemetry is configured but cannot export:")
        out(f"  {hint}")
        out("A drop-in install copies the plugin but not its dependencies.")
        if not config.active:
            out("Settings are missing too; run `hermes axiom setup` after installing it.")
        return 1
    if not config.active:
        out("Axiom telemetry is idle:")
        for problem in config.problems():
            out(f"  {problem}")
        out("Run `hermes axiom setup` to fix it.")
        return 1
    out(f"Axiom telemetry is on, sending to {config.domain}.")
    out(f"  signals   {', '.join(config.configured_signals)}")
    out(f"  redaction {config.redaction}")
    if config.org:
        out(f"  org       {config.org}")
    if config.claim_url:
        out("")
        out(f"This org is unclaimed and expires {config.expires_at}. Claim it:")
        out(f"  {config.claim_url}")
    return 0
