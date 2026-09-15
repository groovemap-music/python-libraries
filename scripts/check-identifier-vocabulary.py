"""Verify the vendored identifier vocabulary, schemas, and fixtures match their recorded digests."""

import json
from hashlib import sha256
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_RECORDS = (
    ROOT / "src" / "common" / "identifier_vocabulary" / "source.json",
    ROOT / "tests" / "fixtures" / "identifiers" / "source.json",
)


def check_source_record(source_record: Path) -> None:
    """Verify every file listed in one source.json against its recorded digest."""
    assert source_record.is_file(), f"identifier vocabulary source record is missing: {source_record}"

    source = json.loads(source_record.read_text())
    for key in ("repository", "commit", "files"):
        assert key in source, f"{source_record} is missing required key: {key}"
    assert len(source["commit"]) == 40, f"{source_record} commit must be a full 40-character SHA"
    assert source["files"], f"{source_record} must record at least one vendored file"

    for entry in source["files"]:
        for key in ("path", "vendored_as", "sha256"):
            assert key in entry, f"{source_record} file entry is missing required key: {key}"

        vendored_file = ROOT / entry["vendored_as"]
        assert vendored_file.is_file(), f"vendored identifier file is missing: {vendored_file}"

        actual_digest = sha256(vendored_file.read_bytes()).hexdigest()
        assert actual_digest == entry["sha256"], (
            f"identifier vocabulary digest drift for {entry['vendored_as']}: expected {entry['sha256']} "
            f"(recorded in {source_record} for {source['repository']}@{source['commit']}), got "
            f"{actual_digest}. Re-vendor the file from the design repository and update {source_record}."
        )

        print(f"identifier vocabulary digest verified: {actual_digest} ({entry['vendored_as']})")


def main() -> None:
    """Check every recorded identifier vocabulary and fixture source record."""
    for source_record in SOURCE_RECORDS:
        check_source_record(source_record)


if __name__ == "__main__":
    main()
