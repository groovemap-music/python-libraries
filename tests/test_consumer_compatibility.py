"""Contract tests for public-consumer and credential-removal evidence."""

import copy
import json
import runpy
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MATRIX = json.loads((ROOT / "docs/consumer-compatibility.json").read_text())
PACKAGE_CONTRACT = runpy.run_path(ROOT / "scripts/verify-consumer-compatibility.py")["package_contract"]


def _toml(path: Path) -> dict:
    with path.open("rb") as file:
        return tomllib.load(file)


def test_consumer_matrix_has_exact_reviewed_scope() -> None:
    """The evidence must neither omit a consumer nor widen secret scope."""
    consumers = {consumer["repository"] for consumer in MATRIX["consumers"]}
    assert consumers == set(MATRIX["credential_removal"]["consumer_set"])
    assert len(consumers) == 10
    assert all(consumer["result"] == "passed" for consumer in MATRIX["consumers"])


def test_consumer_matrix_records_exact_package_revision() -> None:
    """Every result is tied to one immutable Python 3.14 package revision."""
    assert len(MATRIX["library"]["revision"]) == 40
    assert MATRIX["library"]["python"] == "3.14.5"
    assert MATRIX["verification"]["consumer_command"] == "just check"
    assert MATRIX["verification"]["result"] == "passed"


def test_tool_only_coverage_policy_does_not_invalidate_package_evidence() -> None:
    """Coverage settings do not alter either built distribution or its consumer contract."""
    current = _toml(ROOT / "pyproject.toml")
    tool_only_change = copy.deepcopy(current)
    tool_only_change["tool"]["coverage"]["report"]["fail_under"] += 1
    package_change = copy.deepcopy(current)
    package_change["project"]["version"] = "999.0.0"

    assert PACKAGE_CONTRACT(tool_only_change) == PACKAGE_CONTRACT(current)
    assert PACKAGE_CONTRACT(package_change) != PACKAGE_CONTRACT(current)


def test_historical_credential_removal_evidence_records_original_gate() -> None:
    """The immutable pre-cutover evidence retains its original approval boundary."""
    removal = MATRIX["credential_removal"]
    assert removal["performed"] is False
    assert removal["ready_after_publication"] is True
    assert removal["required_visibility"] == "public"
    assert set(removal["resource_families"]) == {
        "github_actions_secret.ci_app_private_key",
        "github_actions_variable.ci_app_client_id",
        "github_dependabot_secret.ci_app_private_key",
    }
    assert "the exact OpenTofu plan receives separate operator approval" in removal["preconditions"]


def test_documentation_names_the_reproducible_and_remote_gates() -> None:
    """Operators receive both the historical proof and the current public status."""
    documentation = (ROOT / "docs/consumer-compatibility.md").read_text()
    normalized = " ".join(documentation.split())
    assert "verify-consumer-compatibility.py" in normalized
    assert "anonymous HTTPS fetch" in normalized
    assert "separately reviewed OpenTofu plan" in normalized
    assert "**Public-library cutover: complete.**" in normalized
    assert "immutable pre-cutover evidence snapshot" in normalized
    assert "```mermaid" in documentation


def test_active_guidance_keeps_private_access_dormant() -> None:
    """Public consumption must not regress into required private-package credentials."""
    authentication = (ROOT / "private-package-auth.md").read_text()
    documentation_index = (ROOT / "docs/README.md").read_text()
    workflows = "\n".join(path.read_text() for path in (ROOT / ".github/workflows").glob("*.yml"))
    normalized_authentication = " ".join(authentication.split())

    assert "**Public-library cutover: complete.**" in authentication
    assert "No private-package credential is required" in normalized_authentication
    assert "## Dormant automation compatibility" in authentication
    assert "They default off or empty" in normalized_authentication
    assert "requires-private-library" in authentication
    assert "Completed public-library cutover" in documentation_index
    assert "requires-private-library" not in workflows
    for stale_instruction in (
        "gh auth setup-git",
        "Do not remove the temporary credentials until",
        "repository's current private visibility",
    ):
        assert stale_instruction not in authentication
        assert stale_instruction not in documentation_index
        assert stale_instruction not in (ROOT / "docs/consumer-compatibility.md").read_text()


def test_historical_verifier_accepts_current_source_changes(tmp_path, monkeypatch) -> None:
    """Historical evidence validates its commit even when current packages evolve."""
    import subprocess

    import pytest

    verifier = runpy.run_path(ROOT / "scripts/verify-consumer-compatibility.py")
    validate = verifier["validate_library_revision"]
    original_run = verifier["run"]
    monkeypatch.setitem(validate.__globals__, "run", lambda *command: original_run(*command, cwd=tmp_path))

    def git(*args: str) -> str:
        return subprocess.run(  # noqa: S603 — fixed test commands in a disposable repository
            [verifier["GIT"], *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "--quiet")
    for metadata, source, name in (
        ("pyproject.toml", "src/common", "groovemap-runtime"),
        ("agent-tools/pyproject.toml", "agent-tools/src/common/agent_tools", "groovemap-agent-tools"),
    ):
        (tmp_path / metadata).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / metadata).write_text(
            f'[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n[project]\nname = "{name}"\nversion = "0.1.0"\n'
        )
        (tmp_path / source).mkdir(parents=True)
        (tmp_path / source / "__init__.py").write_text('VERSION = "historical"\n')
    git("add", ".")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        "feat: historical packages",
    )
    revision = git("rev-parse", "HEAD")
    matrix = copy.deepcopy(MATRIX)
    matrix["library"]["revision"] = revision
    (tmp_path / "src/common/__init__.py").write_text('VERSION = "current"\n')
    (tmp_path / "pyproject.toml").write_text("invalid current metadata deliberately ignored")
    git("add", ".")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        "fix: evolve current source independently of historical evidence",
    )
    assert git("rev-parse", "HEAD") != revision
    validate(matrix)

    # Forged historical package evidence and absent revisions must still fail.
    matrix["library"]["packages"][0] = "groovemap-runtime==9.9.9"
    with pytest.raises(AssertionError, match="historical package contract"):
        validate(matrix)
    matrix["library"]["revision"] = "0" * 40
    with pytest.raises(subprocess.CalledProcessError):
        validate(matrix)

    # A real commit lacking one shipped package is not sufficient evidence.
    (tmp_path / "pyproject.toml").write_text(git("show", f"{revision}:pyproject.toml"))
    git("add", "pyproject.toml")
    git("rm", "--quiet", "agent-tools/src/common/agent_tools/__init__.py")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        "fix: remove recorded package",
    )
    matrix["library"]["revision"] = git("rev-parse", "HEAD")
    with pytest.raises(subprocess.CalledProcessError):
        validate(matrix)


def test_historical_matrix_verifier_rejects_widened_consumer_scope() -> None:
    """Historical-only validation retains the exact ten-consumer boundary."""
    import pytest

    validate = runpy.run_path(ROOT / "scripts/verify-consumer-compatibility.py")["validate_matrix"]
    validate(MATRIX)
    widened = copy.deepcopy(MATRIX)
    widened["consumers"][0]["repository"] = "unreviewed-consumer"
    with pytest.raises(AssertionError):
        validate(widened)
