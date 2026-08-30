set shell := ["bash", "-euo", "pipefail", "-c"]

default:
    @just --list

setup:
    uv sync --all-packages --all-extras --dev --frozen

# A clean checkout must provision the locked workspace before validation. In particular,
# mypy follows imports into every supported optional integration, so validation needs the
# same all-extras environment used by package and install checks.
check: setup format-check lint typecheck test automation-check consumer-matrix-check build distribution-check install-check license-check secret-scan bump-preview

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
    uv run pytest -m "not integration" --cov=common --cov-report=term-missing --cov-report=xml

coverage:
    uv run pytest -m "not integration" --cov=common --cov-report=term-missing --cov-report=xml

test-integration:
    uv run pytest -m integration

build:
    uv build --all-packages --out-dir dist --clear

distribution-check: build
    uv run python scripts/check-distributions.py

install-check: build
    bash scripts/install-check.sh

license-check:
    uv run python scripts/check-licenses.py
    uv run pip-licenses --fail-on "GPL-2.0-only;GPL-3.0-only;AGPL-3.0-only"

secret-scan:
    gitleaks git --redact --no-banner
    gitleaks dir . --redact --no-banner

automation-check:
    actionlint .github/workflows/*.yml
    uv run python scripts/check-automation.py

consumer-matrix-check:
    uv run python scripts/verify-consumer-compatibility.py

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
