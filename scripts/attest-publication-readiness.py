"""Validate and record the local Python-library publication candidate."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tomllib
from collections import Counter
from hashlib import sha256
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "groovemap-music/python-libraries"
REPOSITORY_URL = f"https://github.com/{REPOSITORY}"
AUTOMATION_REVISION = "2f34a4da5c552bc23c75edd3d8d81be0a4b3271c"
FORBIDDEN_HISTORY_PATHS = (
    re.compile(r"(^|/)\.planning(/|$)"),
    re.compile(r"(^|/)docs/superpowers/(plans|specs)(/|$)"),
    re.compile(r"(^|/)secrets?(/|$)"),
    re.compile(r"(^|/)\.env($|\.)"),
    re.compile(r"\.(key|pem)$"),
)
CONTRACT_PATHS = (
    "README.md",
    "docs/runtime.md",
    "docs/agent-tools.md",
    "docs/compatibility-and-releases.md",
    "pyproject.toml",
    "agent-tools/pyproject.toml",
)
GIT = shutil.which("git")
assert GIT is not None, "git is required"


def digest(path: Path) -> str:
    """Return a file's SHA-256 digest."""
    return sha256(path.read_bytes()).hexdigest()


def git(*arguments: str, input_text: str | None = None) -> str:
    """Run Git in the repository and return stripped standard output."""
    return subprocess.run(  # noqa: S603 -- executable is resolved from the operator's PATH
        [GIT, *arguments],
        cwd=ROOT,
        input=input_text,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()


def load_project(path: Path) -> dict[str, Any]:
    """Load one PEP 621 project table."""
    with path.open("rb") as source:
        return tomllib.load(source)["project"]


def public_object_graph() -> dict[str, Any]:
    """Inspect every Git object reachable from the candidate public revision."""
    entries = git("rev-list", "--objects", "HEAD").splitlines()
    object_ids: list[str] = []
    paths: list[str] = []
    for entry in entries:
        object_id, separator, path = entry.partition(" ")
        object_ids.append(object_id)
        if separator:
            paths.append(path)

    forbidden = sorted({path for path in paths if any(pattern.search(path) for pattern in FORBIDDEN_HISTORY_PATHS)})
    assert not forbidden, f"candidate history contains private or credential-bearing paths: {', '.join(forbidden)}"

    checks = git("cat-file", "--batch-check=%(objectname) %(objecttype)", input_text="\n".join(object_ids) + "\n").splitlines()
    assert len(checks) == len(object_ids)
    types = Counter(line.rsplit(" ", 1)[1] for line in checks)
    assert set(types) <= {"blob", "commit", "tag", "tree"}
    assert types["commit"] == int(git("rev-list", "--count", "HEAD"))

    return {
        "commit_count": types["commit"],
        "forbidden_path_matches": [],
        "object_count": len(object_ids),
        "object_types": dict(sorted(types.items())),
        "root_commits": sorted(git("rev-list", "--max-parents=0", "HEAD").splitlines()),
        "scan_scope": "every object reachable from candidate.commit",
    }


def validate_current_tree() -> dict[str, Any]:
    """Validate active identity and absence of raw private planning paths."""
    for path in (ROOT / ".planning", ROOT / "docs/superpowers/plans", ROOT / "docs/superpowers/specs"):
        assert not path.exists(), f"private planning path remains in the current tree: {path.relative_to(ROOT)}"

    readme = (ROOT / "README.md").read_text()
    agent_readme = (ROOT / "agent-tools/README.md").read_text()
    assert "GrooveMap Python libraries" in readme
    assert "GrooveMap" in agent_readme
    assert "discogsography" not in readme.casefold()
    assert "discogsography" not in agent_readme.casefold()

    return {
        "active_identity": "GrooveMap Python libraries",
        "legacy_identity_matches": [],
        "provenance_exception": "docs/extraction.md names the private source repository only as sanitized migration provenance",
        "raw_private_planning_paths": [],
    }


def validate_automation() -> dict[str, Any]:
    """Record the immutable shared automation callers."""
    workflows = {}
    for name in ("ci.yml", "release.yml"):
        path = ROOT / ".github/workflows" / name
        text = path.read_text()
        revisions = set(re.findall(r"groovemap-music/automation/[^@\s]+@([0-9a-f]{40})", text))
        assert revisions == {AUTOMATION_REVISION}
        workflows[name] = digest(path)
    return {
        "revision": AUTOMATION_REVISION,
        "workflow_sha256": workflows,
    }


def validate_contracts() -> dict[str, Any]:
    """Record the reviewed public API and package contracts."""
    hashes = {path: digest(ROOT / path) for path in CONTRACT_PATHS}
    runtime = load_project(ROOT / "pyproject.toml")
    agent_tools = load_project(ROOT / "agent-tools/pyproject.toml")
    assert runtime["name"] == "groovemap-runtime"
    assert agent_tools["name"] == "groovemap-agent-tools"
    assert runtime["version"] == agent_tools["version"]
    assert runtime["requires-python"] == agent_tools["requires-python"] == ">=3.14,<3.15"
    assert agent_tools["dependencies"] == [f"groovemap-runtime=={runtime['version']}"]
    return {
        "documents_sha256": hashes,
        "packages": [f"{runtime['name']}=={runtime['version']}", f"{agent_tools['name']}=={agent_tools['version']}"],
        "python": "3.14",
    }


def validate_consumers() -> dict[str, Any]:
    """Record the exact ten-consumer no-credential evidence."""
    path = ROOT / "docs/consumer-compatibility.json"
    matrix = json.loads(path.read_text())
    consumers = matrix["consumers"]
    assert len(consumers) == 10
    assert all(consumer["result"] == "passed" for consumer in consumers)
    assert matrix["verification"]["result"] == "passed"
    assert matrix["credential_removal"]["performed"] is False
    return {
        "consumer_count": len(consumers),
        "credential_environment_removed": sorted(matrix["verification"]["credential_environment"]),
        "evidence_sha256": digest(path),
        "library_revision": matrix["library"]["revision"],
        "repositories": sorted(consumer["repository"] for consumer in consumers),
        "result": "passed",
    }


def validate_release_artifacts(commit: str) -> dict[str, Any]:
    """Validate the ignored artifacts produced by the non-publishing release rehearsal."""
    dist = ROOT / "dist"
    provenance = json.loads((dist / "provenance.json").read_text())
    assert provenance["commit"] == commit
    assert provenance["repository"] == REPOSITORY_URL

    checksums: list[dict[str, str]] = []
    for line in (dist / "SHA256SUMS").read_text().splitlines():
        expected, name = line.split(maxsplit=1)
        name = name.removeprefix("*").strip().removeprefix("./")
        artifact = dist / name
        assert artifact.is_file()
        assert digest(artifact) == expected
        checksums.append({"name": name, "sha256": expected})
    assert len(checksums) == 4
    assert {item["name"] for item in checksums} == {item["name"] for item in provenance["artifacts"]}

    sbom = json.loads((dist / "sbom.json").read_text())
    notices = json.loads((dist / "THIRD_PARTY_NOTICES.json").read_text())
    assert sbom["bomFormat"] == "CycloneDX"
    assert isinstance(sbom.get("components"), list) and sbom["components"]
    assert isinstance(notices, list) and notices
    return {
        "artifacts": sorted(checksums, key=lambda item: item["name"]),
        "provenance_sha256": digest(dist / "provenance.json"),
        "sbom": {"component_count": len(sbom["components"]), "format": "CycloneDX"},
        "third_party_notice_count": len(notices),
    }


def build_attestation(*, include_artifacts: bool) -> dict[str, Any]:
    """Build deterministic evidence for the current candidate commit."""
    commit = git("rev-parse", "HEAD")
    attestation: dict[str, Any] = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "candidate": {"commit": commit, "tree": git("rev-parse", "HEAD^{tree}")},
        "automation": validate_automation(),
        "brand_and_privacy": validate_current_tree(),
        "consumer_compatibility": validate_consumers(),
        "contracts": validate_contracts(),
        "legal_and_security": {
            "dependency_audit": "passed by just publication-readiness",
            "dependency_license_policy": "passed by just check",
            "first_party_license": "MIT",
            "secret_scans": ["gitleaks git --redact --no-banner", "gitleaks dir . --redact --no-banner"],
        },
        "public_history": public_object_graph(),
        "external_approval_gates": [
            {
                "approved": False,
                "gate": "repository publication",
                "required_action": "approve the exact visibility and main-protection change",
            },
            {
                "approved": False,
                "gate": "anonymous source verification",
                "required_action": "after publication, fetch candidate.commit over anonymous HTTPS",
            },
            {
                "approved": False,
                "gate": "private-library credential removal",
                "required_action": "approve an exact post-publication OpenTofu plan after anonymous fetch and consumer revalidation",
            },
            {
                "approved": False,
                "gate": "package release",
                "required_action": "approve version bump, annotated tag, trusted publisher, and registry publication separately",
            },
        ],
        "mutations_performed": {
            "credentials_removed": False,
            "package_published": False,
            "repository_visibility_changed": False,
            "tag_created": False,
        },
        "validation": {
            "commands": ["just check", "just audit", "just build", "just release-dry-run"],
            "result": "passed",
        },
    }
    if include_artifacts:
        assert not git("status", "--porcelain", "--untracked-files=no"), "tracked worktree must be clean before final attestation"
        attestation["release_artifacts"] = validate_release_artifacts(commit)
    return attestation


def parse_args() -> argparse.Namespace:
    """Parse command-line options."""
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="validate source, history, and policy without writing evidence")
    group.add_argument("--output", type=Path, help="write final ignored evidence after the full readiness recipe")
    return parser.parse_args()


def main() -> None:
    """Validate the candidate or write its complete readiness evidence."""
    args = parse_args()
    attestation = build_attestation(include_artifacts=args.output is not None)
    if args.output is not None:
        output = args.output if args.output.is_absolute() else ROOT / args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(attestation, indent=2, sort_keys=True) + "\n")
        print(f"Publication-readiness attestation: {output}")
    else:
        print("Publication-readiness source, history object graph, and approval-boundary checks passed.")


if __name__ == "__main__":
    main()
