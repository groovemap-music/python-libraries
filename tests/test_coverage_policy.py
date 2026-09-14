"""Executable contract for the workspace-wide coverage floor."""

import re
import tomllib
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
AGGREGATE_COVERAGE_FLOOR = 93


def _toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as file:
        return tomllib.load(file)


def test_workspace_packages_cannot_weaken_the_aggregate_coverage_floor() -> None:
    """Every package override must preserve the measured aggregate floor."""
    root_config = _toml(REPO_ROOT / "pyproject.toml")
    root_report = root_config["tool"]["coverage"]["report"]

    assert root_report["fail_under"] == AGGREGATE_COVERAGE_FLOOR
    assert root_report["precision"] == 2

    for member in root_config["tool"]["uv"]["workspace"]["members"]:
        member_report = _toml(REPO_ROOT / member / "pyproject.toml")["tool"]["coverage"]["report"]
        assert member_report["fail_under"] >= AGGREGATE_COVERAGE_FLOOR, member
        assert member_report["precision"] == root_report["precision"], member


def test_ci_uses_the_root_aggregate_coverage_gate() -> None:
    """The CI alias must retain one root-configured measurement across the namespace."""
    justfile = (REPO_ROOT / "Justfile").read_text()
    workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
    codecov = (REPO_ROOT / "codecov.yml").read_text()

    assert re.search(r"^test:\n    uv run pytest .* --cov=common ", justfile, flags=re.MULTILINE)
    assert re.search(r"^coverage: test$", justfile, flags=re.MULTILINE)
    assert "--cov-fail-under" not in justfile
    assert "coverage-command: just coverage" in workflow
    assert f"target: {AGGREGATE_COVERAGE_FLOOR}%" in codecov
    assert "threshold: 0%" in codecov
