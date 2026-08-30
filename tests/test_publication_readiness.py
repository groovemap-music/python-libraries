"""Contracts for commit-bound, non-publishing readiness evidence."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_publication_readiness_policy_is_in_the_complete_gate() -> None:
    """The ordinary validation gate must reject stale readiness policy."""
    justfile = (ROOT / "Justfile").read_text()
    check = justfile.split("\n\nformat:", 1)[0]
    assert "publication-readiness-check" in check
    assert "publication-readiness: audit release-dry-run" in justfile
    assert "dist/publication-readiness.json" in justfile


def test_publication_readiness_source_and_history_are_clean() -> None:
    """The checked-in candidate has a complete reachable-object policy scan."""
    completed = subprocess.run(
        [sys.executable, "scripts/attest-publication-readiness.py", "--check"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "object" in completed.stdout
    assert "approval-boundary" in completed.stdout


def test_publication_documentation_keeps_remote_mutations_gated() -> None:
    """Readiness must state every remaining external decision and prohibited side effect."""
    documentation = (ROOT / "docs/publication-readiness.md").read_text()
    for marker in (
        "```mermaid",
        "repository visibility to public and protect",
        "unauthenticated environment",
        "exact OpenTofu plan",
        "annotated tag",
        "never commits, tags, pushes, publishes, changes visibility, or removes a",
    ):
        assert marker in documentation
