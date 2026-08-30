"""Contract tests for public-consumer and credential-removal evidence."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MATRIX = json.loads((ROOT / "docs/consumer-compatibility.json").read_text())


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


def test_credential_removal_requires_publication_and_approval() -> None:
    """Compatibility evidence cannot itself authorize an external mutation."""
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
    """Operators receive both the local proof and the post-publication boundary."""
    documentation = (ROOT / "docs/consumer-compatibility.md").read_text()
    assert "verify-consumer-compatibility.py" in documentation
    assert "anonymous HTTPS fetch" in documentation
    assert "separately reviewed OpenTofu plan" in documentation
    assert "```mermaid" in documentation
