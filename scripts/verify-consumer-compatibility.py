"""Validate and optionally reproduce the no-credential consumer matrix."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
from hashlib import sha256
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "docs/consumer-compatibility.json"
PUBLIC_SOURCE = "https://github.com/groovemap-music/python-libraries.git"
ORIGINAL_LIBRARY_REVISION = "28fa329702bc76896cc54ab8d05ec5b1bd3d929e"
EXPECTED_CONSUMERS = {
    "analytics-engine",
    "catalog-api",
    "database-schema",
    "discogs-graph-enricher",
    "discogs-sql-loader",
    "graph-explorer",
    "mcp-server",
    "musicbrainz-graph-enricher",
    "musicbrainz-sql-loader",
    "operations-console",
}
EXPECTED_RESOURCE_FAMILIES = {
    "github_actions_variable.ci_app_client_id",
    "github_actions_secret.ci_app_private_key",
    "github_dependabot_secret.ci_app_private_key",
}
CREDENTIAL_ENVIRONMENT = {
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GROOVEMAP_CI_APP_CLIENT_ID",
    "GROOVEMAP_CI_APP_PRIVATE_KEY",
}
GIT = shutil.which("git")
JUST = shutil.which("just")
assert GIT is not None, "git is required"
assert JUST is not None, "just is required"


def run(*command: str, cwd: Path = ROOT, env: dict[str, str] | None = None) -> str:
    """Run a checked command and return stripped standard output."""
    completed = subprocess.run(command, cwd=cwd, env=env, check=True, text=True, capture_output=True)  # noqa: S603
    return completed.stdout.strip()


def load_matrix() -> dict[str, Any]:
    """Load the compatibility evidence."""
    return json.loads(MATRIX_PATH.read_text())


def validate_matrix(matrix: dict[str, Any]) -> None:
    """Reject stale, widened, or incomplete evidence."""
    assert matrix["schema_version"] == 1
    library = matrix["library"]
    assert library["repository"] == "groovemap-music/python-libraries"
    assert len(library["revision"]) == 40
    assert library["python"] == "3.14.5"
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert library["packages"] == [f"groovemap-runtime=={version}", f"groovemap-agent-tools=={version}"]

    verification = matrix["verification"]
    assert verification["consumer_command"] == "just check"
    assert verification["public_source"] == PUBLIC_SOURCE
    assert verification["result"] == "passed"
    assert set(verification["credential_environment"]) == CREDENTIAL_ENVIRONMENT

    consumers = matrix["consumers"]
    assert len(consumers) == 10
    assert {consumer["repository"] for consumer in consumers} == EXPECTED_CONSUMERS
    for consumer in consumers:
        assert len(consumer["revision"]) == 40
        assert consumer["result"] == "passed"
        assert set(consumer["packages"]) <= {"groovemap-runtime", "groovemap-agent-tools"}
        assert consumer["packages"]

    removal = matrix["credential_removal"]
    assert removal["performed"] is False
    assert removal["ready_after_publication"] is True
    assert removal["required_visibility"] == "public"
    assert set(removal["resource_families"]) == EXPECTED_RESOURCE_FAMILIES
    assert set(removal["consumer_set"]) == EXPECTED_CONSUMERS
    assert "the exact OpenTofu plan receives separate operator approval" in removal["preconditions"]


def validate_library_revision(matrix: dict[str, Any]) -> None:
    """Require the recorded revision and an unchanged package tree after it."""
    revision = matrix["library"]["revision"]
    run(GIT, "cat-file", "-e", f"{revision}^{{commit}}")
    subprocess.run(  # noqa: S603
        [
            GIT,
            "diff",
            "--quiet",
            revision,
            "HEAD",
            "--",
            "pyproject.toml",
            "agent-tools/pyproject.toml",
            "src",
            "agent-tools/src",
        ],
        cwd=ROOT,
        check=True,
    )


def credential_free_environment() -> dict[str, str]:
    """Return a non-interactive environment without private-library credentials."""
    environment = os.environ.copy()
    for name in CREDENTIAL_ENVIRONMENT:
        environment.pop(name, None)
    environment.pop("VIRTUAL_ENV", None)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


def prepare_consumer(source: Path, revision: str, destination: Path, library: Path, library_revision: str) -> None:
    """Create a disposable exact consumer checkout using the local library Git transport."""
    run(GIT, "cat-file", "-e", f"{revision}^{{commit}}", cwd=source)
    run(GIT, "clone", "--no-local", "--quiet", "--no-checkout", str(source), str(destination), cwd=source.parent)
    run(GIT, "checkout", "--quiet", "--detach", revision, cwd=destination)

    local_source = library.resolve().as_uri()
    pinned_paths = run(GIT, "grep", "-Il", ORIGINAL_LIBRARY_REVISION, cwd=destination).splitlines()
    assert pinned_paths, f"{source.name} lacks the reviewed immutable pin"
    for relative in pinned_paths:
        path = destination / relative
        path.write_text(path.read_text().replace(ORIGINAL_LIBRARY_REVISION, library_revision))

    for relative in ("pyproject.toml", "uv.lock"):
        path = destination / relative
        text = path.read_text()
        assert PUBLIC_SOURCE in text, f"{source.name}/{relative} lacks the credential-free public source"
        assert library_revision in text, f"{source.name}/{relative} lacks the overlaid immutable pin"
        path.write_text(text.replace(PUBLIC_SOURCE, local_source))

    for relative in pinned_paths:
        compatibility = destination / relative
        evidence = compatibility.with_name("source.json")
        if compatibility.name != "compatibility.json" or not evidence.exists():
            continue
        source_evidence = json.loads(evidence.read_text())
        source_evidence["contract_sha256"] = sha256(compatibility.read_bytes()).hexdigest()
        evidence.write_text(f"{json.dumps(source_evidence, indent=2)}\n")

    commit_transport_rewrite(destination)


def commit_transport_rewrite(destination: Path) -> None:
    """Commit the overlaid transport so the clone is a normal one-commit-ahead checkout.

    The clone is detached at a reviewed revision that predates its repository's latest
    release tag, so ``git log <tag>..HEAD`` is empty and a consumer's release-preview step
    reports that it found no commits. Leaving the overlay uncommitted also leaves the tree
    dirty, which is not a state any consumer's gate is written for. Committing the overlay
    under a conventional subject fixes both: the release preview sees exactly one new
    commit to classify, and the gate runs against a clean tree. No consumer check is
    relaxed or skipped, and the committed content is byte-identical to the overlay.
    """
    run(GIT, "add", "--all", cwd=destination)
    run(
        GIT,
        "-c",
        "user.name=GrooveMap compatibility rehearsal",
        "-c",
        "user.email=noreply@groovemap.music",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "tag.gpgsign=false",
        "commit",
        "--quiet",
        "--no-verify",
        "-m",
        "fix(deps): resolve groovemap-runtime through the rehearsal transport",
        cwd=destination,
    )


def verify_consumers(matrix: dict[str, Any], workspace: Path, repositories: set[str]) -> None:
    """Run every consumer's complete gate against the exact library revision without credentials."""
    library_revision = matrix["library"]["revision"]
    environment = credential_free_environment()
    with tempfile.TemporaryDirectory(prefix="groovemap-consumer-compatibility-") as temporary:
        temporary_root = Path(temporary)
        library_checkout = temporary_root / "python-libraries"
        run(GIT, "clone", "--no-local", "--quiet", "--no-checkout", str(ROOT), str(library_checkout))
        run(GIT, "checkout", "--quiet", "--detach", library_revision, cwd=library_checkout)
        environment["GROOVEMAP_RUNTIME_REPO"] = str(library_checkout)
        environment["GROOVEMAP_LIBRARIES_REPO"] = str(library_checkout)
        for consumer in matrix["consumers"]:
            name = consumer["repository"]
            if name not in repositories:
                continue
            print(f"==> {name}@{consumer['revision']} against python-libraries@{library_revision}", flush=True)
            checkout = temporary_root / name
            prepare_consumer(workspace / name, consumer["revision"], checkout, library_checkout, library_revision)
            subprocess.run([JUST, "check"], cwd=checkout, env=environment, check=True)  # noqa: S603


def parse_args() -> argparse.Namespace:
    """Parse command-line options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        help="workspace containing the ten sibling consumer repositories; omit for portable evidence checks",
    )
    parser.add_argument(
        "--repository",
        action="append",
        choices=sorted(EXPECTED_CONSUMERS),
        help="rehearse only this consumer (repeatable); defaults to all ten",
    )
    return parser.parse_args()


def main() -> None:
    """Validate portable evidence and optionally reproduce all consumer gates."""
    args = parse_args()
    matrix = load_matrix()
    validate_matrix(matrix)
    validate_library_revision(matrix)
    if args.workspace is not None:
        workspace = args.workspace.resolve()
        repositories = set(args.repository or EXPECTED_CONSUMERS)
        missing = sorted(name for name in repositories if not (workspace / name / ".git").exists())
        assert not missing, f"workspace is missing consumer repositories: {', '.join(missing)}"
        verify_consumers(matrix, workspace, repositories)
    print("Consumer compatibility evidence is internally consistent.")


if __name__ == "__main__":
    main()
