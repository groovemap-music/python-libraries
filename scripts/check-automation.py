"""Validate immutable, actor-independent GitHub automation callers."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUTOMATION_REVISION = "2f34a4da5c552bc23c75edd3d8d81be0a4b3271c"


def require_markers(text: str, markers: set[str], subject: str) -> None:
    """Require every declared automation contract marker."""
    missing = sorted(marker for marker in markers if marker not in text)
    assert not missing, f"{subject} is missing required markers: {', '.join(missing)}"


def validate_ci() -> None:
    """Require one complete CI caller for every supported trigger and actor."""
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    require_markers(
        workflow,
        {
            "pull_request:",
            "branches: [main]",
            "schedule:",
            "workflow_dispatch:",
            f"groovemap-music/automation/.github/workflows/reusable-ci.yml@{AUTOMATION_REVISION}",
            "language: python",
            "setup-command: just setup",
            "check-command: just check",
            "coverage-command: just coverage",
            "audit-command: just audit",
            "license-command: just license-check",
            "secret-scan-command: just secret-scan",
            "package-command: just build",
            "install-command: just install-check",
            "coverage-files: coverage.xml",
            "upload-codecov: true",
            "CODECOV_TOKEN: ${{ secrets.CODECOV_TOKEN }}",
        },
        "CI workflow",
    )
    lowered = workflow.lower()
    for forbidden in ("github.actor", "dependabot[bot]", "pull_request_target", "secrets: inherit", "if:"):
        assert forbidden not in lowered, f"CI must not contain an actor-specific or reduced path: {forbidden}"
    jobs = workflow.split("jobs:\n", 1)[1]
    job_count = sum(line.startswith("  ") and not line.startswith("    ") and line.rstrip().endswith(":") for line in jobs.splitlines())
    assert job_count == 1, "ordinary and Dependabot pull requests must share one required job graph"


def validate_release() -> None:
    """Require a tag-only, attested package release caller."""
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    require_markers(
        workflow,
        {
            'tags: ["v*"]',
            "attestations: write",
            "contents: read",
            "id-token: write",
            f"groovemap-music/automation/.github/workflows/reusable-release.yml@{AUTOMATION_REVISION}",
            "repository-name: python-libraries",
            "setup-command: just setup",
            "check-command: just check",
            "release-command: just release-dry-run",
            "dist/*.whl",
            "dist/*.tar.gz",
            "dist/provenance.json",
        },
        "release workflow",
    )
    assert "workflow_dispatch:" not in workflow
    assert "branches:" not in workflow


def validate_dependabot() -> None:
    """Require updates for workflow pins and both locked Python packages."""
    config = (ROOT / ".github/dependabot.yml").read_text()
    require_markers(
        config,
        {
            "package-ecosystem: github-actions",
            "labels: [dependencies, github-actions]",
            "package-ecosystem: uv",
            "directories: [/, /agent-tools]",
            "labels: [dependencies, python]",
            "interval: weekly",
        },
        "Dependabot configuration",
    )


def validate_repository() -> None:
    """Validate automation surfaces and reject retired bot workflows."""
    validate_ci()
    validate_release()
    validate_dependabot()
    workflow_names = {path.name.lower() for path in (ROOT / ".github/workflows").iterdir() if path.is_file()}
    assert not any("renovate" in name or "claude" in name for name in workflow_names)


if __name__ == "__main__":
    validate_repository()
