# SPDX-License-Identifier: Apache-2.0 OR MIT
"""Structural guards for the drop-in and pip install paths."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import yaml

import hermess_metrics

PACKAGE_DIR = Path(hermess_metrics.__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
ENTRY_POINT_GROUP = "hermes_agent.plugins"
DISTRIBUTION = "hermess-metrics"


def _manifest() -> dict[str, object]:
    return yaml.safe_load((PACKAGE_DIR / "plugin.yaml").read_text())


def _pyproject() -> dict[str, object]:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())


def test_manifest_sits_beside_the_module() -> None:
    assert (PACKAGE_DIR / "plugin.yaml").is_file()
    assert (PACKAGE_DIR / "__init__.py").is_file()


def test_manifest_name_matches_the_distribution() -> None:
    assert _manifest()["name"] == DISTRIBUTION


def test_manifest_version_matches_the_package() -> None:
    assert str(_manifest()["version"]) == hermess_metrics.__version__


def test_manifest_declares_hooks_under_the_key_the_loader_reads() -> None:
    manifest = _manifest()
    assert isinstance(manifest["provides_hooks"], list)


def test_entry_point_resolves_to_the_package() -> None:
    entry_points = _pyproject()["project"]["entry-points"]  # type: ignore[index]
    assert entry_points[ENTRY_POINT_GROUP] == {DISTRIBUTION: "hermess_metrics"}


def test_distribution_version_matches_the_package() -> None:
    assert _pyproject()["project"]["version"] == hermess_metrics.__version__  # type: ignore[index]


def test_internal_imports_are_relative() -> None:
    """The drop-in path binds the package elsewhere, so self-imports must be relative."""
    offenders: list[str] = []
    for source in sorted(PACKAGE_DIR.rglob("*.py")):
        tree = ast.parse(source.read_text(), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "hermess_metrics":
                        offenders.append(f"{source.name}:{node.lineno} import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level == 0 and module.split(".")[0] == "hermess_metrics":
                    offenders.append(f"{source.name}:{node.lineno} from {module}")
    assert offenders == []


def test_the_manifest_declares_every_hook_the_runtime_subscribes_to() -> None:
    from hermess_metrics.runtime import FLUSH_HOOK, HOOK_KINDS

    declared = set(_manifest()["provides_hooks"])  # type: ignore[arg-type]
    assert declared == set(HOOK_KINDS) | {FLUSH_HOOK}
