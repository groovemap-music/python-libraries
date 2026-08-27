set shell := ["bash", "-euo", "pipefail", "-c"]

default:
    @just --list

setup:
    uv sync --all-packages --all-extras --dev --frozen

check: format-check lint typecheck test build install-check license-check secret-scan bump-preview

format:
    uv run ruff format .
    uv run ruff check --fix .

format-check:
    uv run ruff format --check .

lint:
    uv run ruff check .

typecheck:
    uv run mypy

test:
    uv run pytest -m "not integration"

test-integration:
    uv run pytest -m integration

build:
    uv build --all-packages --out-dir dist --clear

install-check: build
    bash scripts/install-check.sh

license-check:
    uv run python scripts/check-licenses.py
    uv run pip-licenses --fail-on "GPL-2.0-only;GPL-3.0-only;AGPL-3.0-only"

secret-scan:
    gitleaks git --redact --no-banner
    gitleaks dir . --redact --no-banner

audit:
    uv run pip-audit

bump-preview:
    uv run cz bump --dry-run --changelog --yes --check-consistency

# Update local version metadata and changelog only; do not commit, tag, push, or publish.
bump:
    uv run cz bump --version-files-only --changelog --yes --check-consistency
    uv lock

release-dry-run: check
    bash scripts/release-dry-run.sh
