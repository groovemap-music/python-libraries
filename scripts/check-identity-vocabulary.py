"""Verify the vendored identity vocabulary has not drifted from its recorded source digest."""

import json
from hashlib import sha256
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "src" / "common" / "identity_vocabulary"
SOURCE_RECORD = PACKAGE_DIR / "source.json"

assert SOURCE_RECORD.is_file(), f"identity vocabulary source record is missing: {SOURCE_RECORD}"

source = json.loads(SOURCE_RECORD.read_text())
for key in ("repository", "commit", "files"):
    assert key in source, f"source.json is missing required key: {key}"
assert len(source["commit"]) == 40, "source.json commit must be a full 40-character SHA"
assert source["files"], "source.json must record at least one vendored file"

for entry in source["files"]:
    for key in ("path", "vendored_as", "sha256"):
        assert key in entry, f"source.json file entry is missing required key: {key}"

    vendored_file = ROOT / entry["vendored_as"]
    assert vendored_file.is_file(), f"vendored identity vocabulary file is missing: {vendored_file}"

    actual_digest = sha256(vendored_file.read_bytes()).hexdigest()
    assert actual_digest == entry["sha256"], (
        f"identity vocabulary digest drift for {entry['vendored_as']}: expected {entry['sha256']} "
        f"(recorded in source.json for {source['repository']}@{source['commit']}), got {actual_digest}. "
        "Re-vendor the file from the design repository and update source.json."
    )

    print(f"identity vocabulary digest verified: {actual_digest} ({entry['vendored_as']})")
