"""Verify the vendored media taxonomy has not drifted from its recorded source digest."""

import json
from hashlib import sha256
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "src" / "common" / "media_taxonomy"
VENDORED_FILE = PACKAGE_DIR / "media-taxonomy.json"
SOURCE_RECORD = PACKAGE_DIR / "source.json"

assert VENDORED_FILE.is_file(), f"vendored media taxonomy is missing: {VENDORED_FILE}"
assert SOURCE_RECORD.is_file(), f"media taxonomy source record is missing: {SOURCE_RECORD}"

source = json.loads(SOURCE_RECORD.read_text())
for key in ("repository", "commit", "path", "vendored_as", "sha256"):
    assert key in source, f"source.json is missing required key: {key}"
assert len(source["commit"]) == 40, "source.json commit must be a full 40-character SHA"
assert source["vendored_as"] == "src/common/media_taxonomy/media-taxonomy.json"

actual_digest = sha256(VENDORED_FILE.read_bytes()).hexdigest()
assert actual_digest == source["sha256"], (
    f"vendored media taxonomy digest drift: expected {source['sha256']} (recorded in "
    f"source.json for {source['repository']}@{source['commit']}), got {actual_digest}. "
    "Re-vendor the file from the design repository and update source.json."
)

print(f"media taxonomy digest verified: {actual_digest} ({source['repository']}@{source['commit']})")
