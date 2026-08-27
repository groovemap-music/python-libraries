"""Regression tests for independently installable shared Python packages."""

import tomllib
from importlib import import_module
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]


def _toml(relative_path: str) -> dict[str, Any]:
    with (REPO_ROOT / relative_path).open("rb") as file:
        return tomllib.load(file)


def _locked_package(name: str) -> dict[str, Any]:
    packages: list[dict[str, Any]] = _toml("uv.lock")["package"]
    return next(package for package in packages if package["name"] == name)


def test_runtime_metrics_dependency_is_optional_and_in_all() -> None:
    """The lazy metrics endpoint must be installable through a named capability."""
    extras = _toml("pyproject.toml")["project"]["optional-dependencies"]

    assert extras["metrics"] == ["prometheus-client>=0.26.0"]
    assert "prometheus-client>=0.26.0" in extras["all"]

    locked_extras = _locked_package("groovemap-runtime")["optional-dependencies"]
    assert locked_extras["metrics"] == [{"name": "prometheus-client"}]
    assert {dependency["name"] for dependency in locked_extras["all"]} >= {"prometheus-client"}

    assert import_module("prometheus_client")
    assert import_module("common.health_server")


def test_agent_tools_uses_the_workspace_runtime() -> None:
    """The second distribution must resolve the synchronized runtime package locally."""
    agent_tools = _toml("agent-tools/pyproject.toml")["project"]
    assert agent_tools["version"] == _toml("pyproject.toml")["project"]["version"]
    assert agent_tools["dependencies"] == ["groovemap-runtime==0.1.0"]

    locked = _locked_package("groovemap-agent-tools")
    assert {dependency["name"] for dependency in locked["dependencies"]} == {"groovemap-runtime"}
    assert import_module("common.agent_tools")
