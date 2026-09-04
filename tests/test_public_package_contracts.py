"""Executable contracts for the two public GrooveMap distributions."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from packaging.specifiers import SpecifierSet
from packaging.version import Version


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_EXPORTS = {
    "AsyncPostgreSQLPool",
    "AsyncResilientConnection",
    "AsyncResilientNeo4jDriver",
    "AsyncResilientPostgreSQL",
    "AsyncResilientRabbitMQ",
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitOpenError",
    "CircuitState",
    "ConnectionEstablishmentError",
    "DatabaseUnavailableError",
    "ExponentialBackoff",
    "HealthServer",
    "OutageBackoff",
    "ResilientConnection",
    "ResilientNeo4jDriver",
    "ResilientPostgreSQLPool",
    "ResilientRabbitMQConnection",
    "async_resilient_connection",
    "describe_exception",
    "execute_sql",
    "families_of",
    "family_ids",
    "flatten_descriptions",
    "get_meter",
    "instrument_fastapi_app",
    "instrument_httpx",
    "is_db_profiling",
    "is_debug",
    "legacy_format_names_to_media",
    "log_cypher_query",
    "log_sql_query",
    "map_discogs_formats",
    "map_musicbrainz_release",
    "medium_ids",
    "medium_label",
    "neo4j_security_kwargs",
    "normalize_record",
    "parse_postgres_host_port",
    "process_message_with_retry",
    "resilient_connection",
    "setup_logging",
    "setup_telemetry",
    "shutdown_telemetry",
    "with_async_neo4j_retry",
    "with_neo4j_retry",
}
AGENT_TOOLS_EXPORTS = {
    "find_path",
    "get_artist_details",
    "get_collaborators",
    "get_genre_details",
    "get_genre_tree",
    "get_graph_stats",
    "get_label_details",
    "get_release_details",
    "get_style_details",
    "get_trends",
    "media_of",
    "search",
    "validate_media_filter",
}


def _toml(relative_path: str) -> dict[str, Any]:
    with (REPO_ROOT / relative_path).open("rb") as file:
        return tomllib.load(file)


def test_runtime_public_import_contract_is_explicit_and_documented() -> None:
    import common

    assert set(common.__all__) == RUNTIME_EXPORTS
    assert all(getattr(common, name) is not None for name in RUNTIME_EXPORTS)

    contract = (REPO_ROOT / "docs/runtime.md").read_text()
    assert all(f"`{name}`" in contract for name in RUNTIME_EXPORTS)


def test_agent_tools_public_import_contract_is_explicit_and_documented() -> None:
    import common.agent_tools as agent_tools

    assert set(agent_tools.__all__) == AGENT_TOOLS_EXPORTS
    assert all(getattr(agent_tools, name) is not None for name in AGENT_TOOLS_EXPORTS)

    contract = (REPO_ROOT / "docs/agent-tools.md").read_text()
    assert all(f"`{name}`" in contract for name in AGENT_TOOLS_EXPORTS)


def test_distribution_and_console_entry_point_contracts() -> None:
    runtime = _toml("pyproject.toml")["project"]
    agent_tools = _toml("agent-tools/pyproject.toml")["project"]

    assert runtime["name"] == "groovemap-runtime"
    assert agent_tools["name"] == "groovemap-agent-tools"
    assert runtime["requires-python"] == agent_tools["requires-python"] == ">=3.14,<3.15"
    assert runtime["version"] == agent_tools["version"]
    assert agent_tools["dependencies"] == [f"groovemap-runtime=={runtime['version']}"]

    for project in (runtime, agent_tools):
        assert project.get("scripts", {}) == {}
        assert project.get("gui-scripts", {}) == {}
        assert project.get("entry-points", {}) == {}


def test_documented_python_support_matches_the_pinned_ci_lane() -> None:
    mise = _toml(".mise.toml")
    lock = _toml("uv.lock")
    runtime = _toml("pyproject.toml")
    agent_tools = _toml("agent-tools/pyproject.toml")
    active_docs = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "agent-tools/README.md",
        REPO_ROOT / "docs/runtime.md",
        REPO_ROOT / "docs/agent-tools.md",
        REPO_ROOT / "docs/compatibility-and-releases.md",
    ]

    assert mise["tools"]["python"] == "3.14.5"
    # uv normalizes the lock's requires-python (">=3.14,<3.15" becomes "==3.14.*"), so the lock
    # is checked for the interpreter window it admits rather than for one spelling of it.
    locked_python = SpecifierSet(lock["requires-python"])
    assert Version("3.14.5") in locked_python
    assert Version("3.13.9") not in locked_python
    assert Version("3.15.0") not in locked_python
    for package in (runtime, agent_tools):
        assert package["project"]["requires-python"] == ">=3.14,<3.15"
        assert "Programming Language :: Python :: 3.14" in package["project"]["classifiers"]
        assert "Programming Language :: Python :: 3.13" not in package["project"]["classifiers"]
        assert package["tool"]["ruff"]["target-version"] == "py314"
        assert package["tool"]["mypy"]["python_version"] == "3.14"

    assert all("Python 3.14" in document.read_text() for document in active_docs)
    assert all("CI currently verifies Python 3.13" not in document.read_text() for document in active_docs)
    assert "Python 3.14 or later" not in (REPO_ROOT / "docs/compatibility-and-releases.md").read_text()


def test_clean_checkout_validation_provisions_locked_optional_dependencies() -> None:
    justfile = (REPO_ROOT / "Justfile").read_text()

    assert "setup:" in justfile
    assert "uv sync --all-packages --all-extras --dev --frozen" in justfile
    assert re.search(r"^check: setup ", justfile, flags=re.MULTILINE)


def test_readme_build_artifact_contract_matches_build_recipe() -> None:
    justfile = (REPO_ROOT / "Justfile").read_text()
    readme = (REPO_ROOT / "README.md").read_text()

    assert "uv build --all-packages --out-dir dist --clear" in justfile
    assert "written directly to `dist/`" in readme
    assert "dist/runtime" not in readme
    assert "dist/agent-tools" not in readme


def test_contract_documentation_uses_resolving_local_links() -> None:
    documents = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "agent-tools/README.md",
        *(REPO_ROOT / "docs").glob("*.md"),
    ]

    for document in documents:
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", document.read_text()):
            if target.startswith(("https://", "http://", "#")):
                continue
            relative_path = target.split("#", maxsplit=1)[0]
            assert (document.parent / relative_path).resolve().exists(), f"{document}: broken link {target}"


def test_active_contract_docs_use_groovemap_branding() -> None:
    active_documents = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "agent-tools/README.md",
        REPO_ROOT / "docs/README.md",
        REPO_ROOT / "docs/runtime.md",
        REPO_ROOT / "docs/agent-tools.md",
        REPO_ROOT / "docs/compatibility-and-releases.md",
    ]

    assert all("discogsography" not in document.read_text().lower() for document in active_documents)
    assert all("GrooveMap" in document.read_text() or "groovemap" in document.read_text() for document in active_documents)


def test_extraction_provenance_is_portable_and_historical() -> None:
    extraction = (REPO_ROOT / "docs/extraction.md").read_text()

    assert not re.search(r"(?:/Users|/home)/[A-Za-z0-9._-]+/", extraction)
    assert extraction.lower().count("discogsography") == 1
    assert "https://github.com/SimplicityGuy/discogsography.git" in extraction
    assert "204f49e2429f074546dfc67e6354be2529a983ac" in extraction
    assert "28fa329702bc76896cc54ab8d05ec5b1bd3d929e" in extraction
    assert "SOURCE_CHECKOUT='../groovemap-source'" in extraction
    assert "DESTINATION_CHECKOUT='../python-libraries'" in extraction


def test_library_sources_avoid_syntax_newer_than_consumer_type_checkers() -> None:
    """Consumers type-check the installed library source, and not all of them target 3.14 yet.

    PEP 758's parenthesis-free ``except A, B:`` is a syntax error for anything older, so it
    reaches a consumer as a failing `just check` rather than as a lint finding here.
    """
    bare_multi_except = re.compile(r"^\s*except\s+[A-Za-z_][\w.]*\s*,", flags=re.MULTILINE)
    offenders = [
        source.relative_to(REPO_ROOT)
        for source in [*(REPO_ROOT / "src").rglob("*.py"), *(REPO_ROOT / "agent-tools/src").rglob("*.py")]
        if bare_multi_except.search(source.read_text())
    ]

    assert not offenders, f"parenthesize the exception tuple in: {offenders}"
