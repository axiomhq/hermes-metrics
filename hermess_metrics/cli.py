"""The hermes axiom subcommand: set up telemetry and report its state."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
  2) Use an Axiom org you already have. You will need an API token that
     can create datasets.

Choose 1 or 2: """
ORG_QUESTION = "Axiom org id (find it in the console URL; press enter if the token is org-scoped): "

logger = logging.getLogger(__name__)


def hermes_home() -> Path:
    return Path.home() / ".hermes"


def build_parser(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="axiom_action", required=True)
    setup = actions.add_parser("setup", help="Create datasets and a token, then save them")
    setup.add_argument("--token", help="API token for an org you already have")
    setup.add_argument("--org", default="", help="Org id, if your token is not org-scoped")
    setup.add_argument("--provision", action="store_true", help="Provision a new temporary org")
    setup.add_argument("--prefix", default=DEFAULT_PREFIX, help="Dataset name prefix")
    setup.add_argument("--domain", default=DEFAULT_DOMAIN, help="Axiom API host")
    setup.add_argument("--region", help="Edge deployment for a new org")
    setup.add_argument("--env-file", help="Where to write settings")
    actions.add_parser("status", help="Show what the plugin is configured to do")


def register_cli(ctx: Any) -> None:
    ctx.register_cli_command(
        COMMAND,
        "Set up and inspect Axiom telemetry",
        build_parser,
        handle,
        description="Provision or adopt an Axiom org and wire Hermes telemetry to it.",
    )


def handle(args: argparse.Namespace) -> int:
    if getattr(args, "axiom_action", None) == "status":
        return run_status()
    return run_setup(args)


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

    plane = ControlPlane(domain=args.domain)
    try:
        result = provision(plane, prefix=args.prefix, region=args.region, org_token=org_token)
    except AxiomError as exc:
        for line in _explain(exc, org_token, org):
            out(line)
        return 1

    target = Path(args.env_file) if args.env_file else hermes_home() / ENV_FILENAME
    write_env(target, env_values(result))
    _report(result, target, out)
    return 0


TOKEN_SETTINGS_URL = "https://app.axiom.co/settings/api-tokens"


def _explain_denied(exc: AxiomError, org: str) -> list[str]:
    """Say which call was refused, what it needed, and the two usual causes."""
    lines = [f"Setup failed: {exc.operation or 'the request'} was refused ({exc.status})."]
    if exc.message and exc.message != "forbidden":
        lines.append(f"  Axiom said: {exc.message}")
    lines.append("")
    lines.append("Setting up needs a token with both of these permissions:")
    lines.append(f"  {PERMISSION_DATASETS}")
    lines.append(f"  {PERMISSION_TOKENS}")
    lines.append("")
    lines.append("Two things cause this:")
    lines.append(f"  1. The token lacks them. Create one at {TOKEN_SETTINGS_URL}")
    if org:
        lines.append(f"  2. The token does not belong to org {org!r}.")
    else:
        lines.append("  2. The token is not org-scoped and no org was given. Retry with:")
        lines.append("       hermes axiom setup --token <token> --org <org-id>")
    return lines


def _explain(exc: AxiomError, org_token: str | None, org: str = "") -> list[str]:
    """Turn an API failure into something the operator can act on."""
    if exc.status in (401, 403):
        return _explain_denied(exc, org)
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
    if result.needs_claim:
        out("")
        out("Claim the org to keep this data and let alerts fire:")
        out(f"  {result.claim_url}")
        out(f"It is deleted after {result.expires_at} if nobody does.")


def run_status(out: Callable[[str], None] = print) -> int:
    config = Config.from_env()
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
