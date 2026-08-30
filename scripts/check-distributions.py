"""Verify both publishable distributions have accurate legal and source metadata."""

from __future__ import annotations

import tarfile
from email import message_from_bytes
from pathlib import Path
from typing import TYPE_CHECKING
from zipfile import ZipFile

from packaging.specifiers import SpecifierSet


if TYPE_CHECKING:
    from email.message import Message


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
EXPECTED = {
    "groovemap-runtime": ("groovemap_runtime", "src/common/"),
    "groovemap-agent-tools": ("groovemap_agent_tools", "src/common/agent_tools/"),
}
REPOSITORY = "https://github.com/groovemap-music/python-libraries"


def wheel_metadata(path: Path) -> tuple[str, set[str]]:
    """Return the package name and archive members after validating wheel metadata."""
    with ZipFile(path) as archive:
        members = set(archive.namelist())
        metadata_name = next(name for name in members if name.endswith(".dist-info/METADATA"))
        metadata = message_from_bytes(archive.read(metadata_name))
    validate_metadata(metadata)
    return str(metadata["Name"]), members


def validate_metadata(metadata: Message) -> None:
    """Validate the PEP 621 metadata emitted into each distribution."""
    assert metadata["License-Expression"] == "MIT"
    assert SpecifierSet(str(metadata["Requires-Python"])) == SpecifierSet(">=3.14,<3.15")
    assert "Programming Language :: Python :: 3.14" in metadata.get_all("Classifier", [])
    project_urls = metadata.get_all("Project-URL", [])
    assert f"Repository, {REPOSITORY}" in project_urls
    assert "Homepage, https://groovemap.music" in project_urls


def main() -> None:
    """Check the exact two-wheel/two-sdist package matrix."""
    wheels = sorted(DIST.glob("*.whl"))
    sdists = sorted(DIST.glob("*.tar.gz"))
    assert len(wheels) == len(sdists) == len(EXPECTED), "the release must contain two wheels and two sdists"

    found: set[str] = set()
    for wheel in wheels:
        package, members = wheel_metadata(wheel)
        found.add(package)
        normalized, _ = EXPECTED[package]
        assert any(name.endswith(".dist-info/licenses/LICENSE") for name in members), f"{package} wheel omits LICENSE"
        assert any(name.startswith("common/") for name in members), f"{package} wheel omits package sources"
        assert wheel.name.startswith(normalized)

    for sdist in sdists:
        with tarfile.open(sdist) as archive:
            names = set(archive.getnames())
            pkg_info_name = next(name for name in names if name.endswith("/PKG-INFO"))
            pkg_info_file = archive.extractfile(pkg_info_name)
            assert pkg_info_file is not None
            metadata = message_from_bytes(pkg_info_file.read())
        validate_metadata(metadata)
        package = str(metadata["Name"])
        _, source_path = EXPECTED[package]
        assert any(name.endswith("/LICENSE") for name in names), f"{sdist.name} omits LICENSE"
        assert any(f"/{source_path}" in name for name in names), f"{sdist.name} omits package sources"
        if package == "groovemap-runtime":
            assert not any("/src/common/agent_tools/" in name for name in names), "runtime sdist includes agent-tools sources"

    assert found == set(EXPECTED), f"unexpected distribution set: {sorted(found)}"


if __name__ == "__main__":
    main()
