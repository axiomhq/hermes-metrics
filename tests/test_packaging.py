"""Structural guards for the two ways Hermes can load this plugin.

A drop-in install imports the package directory under a synthetic
``hermes_plugins.<slug>`` name and reads ``plugin.yaml`` beside the module. A
pip install resolves the ``hermes_agent.plugins`` entry point instead.
"""

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
    """Absolute self-imports break the drop-in path, where the package is bound
    to ``hermes_plugins.<slug>`` and ``hermess_metrics`` is not importable."""
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
