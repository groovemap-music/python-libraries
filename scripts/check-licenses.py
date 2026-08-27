"""Validate first-party license and synchronized package metadata."""

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def project(path: Path) -> dict[str, object]:
    with path.open("rb") as source:
        return tomllib.load(source)["project"]


runtime = project(ROOT / "pyproject.toml")
agent_tools = project(ROOT / "agent-tools" / "pyproject.toml")

assert runtime["license"] == "MIT"
assert agent_tools["license"] == "MIT"
assert runtime["version"] == agent_tools["version"]
assert agent_tools["dependencies"] == [f"groovemap-runtime=={runtime['version']}"]
assert (ROOT / "LICENSE").read_text().startswith("MIT License\n")
assert (ROOT / "agent-tools" / "LICENSE").read_text().startswith("MIT License\n")
