#!/usr/bin/env bash
set -euo pipefail

runtime_wheel=$(find dist -maxdepth 1 -type f -name 'groovemap_runtime-*.whl' -print -quit)
agent_wheel=$(find dist -maxdepth 1 -type f -name 'groovemap_agent_tools-*.whl' -print -quit)
test -n "$runtime_wheel"
test -n "$agent_wheel"

check_dir=$(mktemp -d)
case "$check_dir" in
/tmp/* | /private/tmp/* | /var/folders/*) ;;
*) echo "Unexpected temporary directory: $check_dir" >&2; exit 2 ;;
esac
trap 'rm -rf -- "$check_dir"' EXIT

uv venv --python 3.14 "$check_dir/venv"
uv pip install --python "$check_dir/venv/bin/python" "$runtime_wheel" "$agent_wheel"
"$check_dir/venv/bin/python" -c 'import common; import common.agent_tools'
