"""Executable contracts for the two public GrooveMap distributions."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any


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
    "is_db_profiling",
    "is_debug",
    "log_cypher_query",
    "log_sql_query",
    "neo4j_security_kwargs",
    "normalize_record",
    "parse_postgres_host_port",
    "process_message_with_retry",
    "resilient_connection",
    "setup_logging",
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
    "search",
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
    assert runtime["requires-python"] == agent_tools["requires-python"] == ">=3.13"
    assert runtime["version"] == agent_tools["version"]
    assert agent_tools["dependencies"] == [f"groovemap-runtime=={runtime['version']}"]

    for project in (runtime, agent_tools):
        assert project.get("scripts", {}) == {}
        assert project.get("gui-scripts", {}) == {}
        assert project.get("entry-points", {}) == {}


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
