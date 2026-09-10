"""The hermes axiom subcommand: set up telemetry and report its state."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Config
from .control_plane import DEFAULT_DOMAIN, AxiomError, ControlPlane
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

logger = logging.getLogger(__name__)


def hermes_home() -> Path:
    return Path.home() / ".hermes"


def build_parser(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="axiom_action", required=True)
    setup = actions.add_parser("setup", help="Create datasets and a token, then save them")
    setup.add_argument("--token", help="API token for an org you already have")
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


def _choose(ask: Callable[[str], str], args: argparse.Namespace) -> str | None:
    """Return the org token to adopt, or None to provision a new org."""
    if args.token:
        return str(args.token)
    if args.provision:
        return None
    answer = ask(QUESTION).strip()
    if answer == CHOICE_PROVISION:
        return None
    if answer == CHOICE_ADOPT:
        token = ask("Axiom API token: ").strip()
        if not token:
            raise ValueError("no token given")
        return token
    raise ValueError(f"expected {CHOICE_PROVISION} or {CHOICE_ADOPT}, got {answer!r}")


def run_setup(
    args: argparse.Namespace,
    ask: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
) -> int:
    try:
        org_token = _choose(ask, args)
    except (ValueError, EOFError, KeyboardInterrupt) as exc:
        out(f"Setup cancelled: {exc}")
        return 2

    plane = ControlPlane(domain=args.domain)
    try:
        result = provision(plane, prefix=args.prefix, region=args.region, org_token=org_token)
    except AxiomError as exc:
        out(f"Setup failed: {exc}")
        return 1

    target = Path(args.env_file) if args.env_file else hermes_home() / ENV_FILENAME
    write_env(target, env_values(result))
    _report(result, target, out)
    return 0


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
