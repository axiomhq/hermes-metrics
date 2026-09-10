"""One call that produces a working Axiom setup and a link."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .config import (
    ENV_CLAIM_URL,
    ENV_DOMAIN,
    ENV_EXPIRES_AT,
    ENV_LOGS_DATASET,
    ENV_METRICS_DATASET,
    ENV_ORG,
    ENV_TOKEN,
    ENV_TRACES_DATASET,
)
from .control_plane import DATASET_KINDS, ControlPlane

DEFAULT_PREFIX = "hermes"
TOKEN_NAME = "hermess-metrics ingest"
DESCRIPTION = "Hermes agent telemetry"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Provisioned:
    """The org, its datasets, the scoped token, and the claim link."""

    org_id: str
    domain: str
    region: str
    expires_at: str
    claim_url: str
    datasets: dict[str, str]
    token: str = field(repr=False)


def provision(
    plane: ControlPlane, prefix: str = DEFAULT_PREFIX, region: str | None = None
) -> Provisioned:
    """Create an org, its three datasets, and an ingest-only token."""
    org = plane.provision_org(name=prefix, region=region)
    admin = ControlPlane(
        domain=plane.domain,
        token=org.token,
        scheme=plane.scheme,
        timeout=plane.timeout,
        backoff=plane.backoff,
    )
    datasets = {signal: f"{prefix}-{signal}" for signal in DATASET_KINDS}
    for signal, name in datasets.items():
        admin.create_dataset(name, DATASET_KINDS[signal], DESCRIPTION)
    token = admin.create_ingest_token(TOKEN_NAME, list(datasets.values()), DESCRIPTION)
    return Provisioned(
        org_id=org.id,
        domain=plane.domain,
        region=org.region,
        expires_at=org.expires_at,
        claim_url=org.claim_url,
        datasets=datasets,
        token=token,
    )


def env_values(provisioned: Provisioned) -> dict[str, str]:
    """The settings the plugin needs, ready to write to an env file."""
    return {
        ENV_TOKEN: provisioned.token,
        ENV_DOMAIN: provisioned.domain,
        ENV_TRACES_DATASET: provisioned.datasets["traces"],
        ENV_LOGS_DATASET: provisioned.datasets["logs"],
        ENV_METRICS_DATASET: provisioned.datasets["metrics"],
        ENV_ORG: provisioned.org_id,
        ENV_CLAIM_URL: provisioned.claim_url,
        ENV_EXPIRES_AT: provisioned.expires_at,
    }


def write_env(path: Path, values: dict[str, str]) -> None:
    """Merge settings into an env file, leaving every other line untouched."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text().splitlines() if path.is_file() else []
    remaining = dict(values)
    merged: list[str] = []
    for line in existing:
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            merged.append(f"{key}={remaining.pop(key)}")
        else:
            merged.append(line)
    merged.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(merged) + "\n")
    path.chmod(0o600)
