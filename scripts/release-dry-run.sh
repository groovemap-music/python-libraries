#!/usr/bin/env bash
set -euo pipefail

uv build --all-packages --out-dir dist --clear
(
  cd dist
  shasum -a 256 ./*.whl ./*.tar.gz > SHA256SUMS
)
uv run cyclonedx-py environment --output-file dist/sbom.json
test -s dist/SHA256SUMS
test -s dist/sbom.json
