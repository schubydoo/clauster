# Clauster dev task runner (make variant). Mirrors the justfile — both run the
# same commands so a contributor can use whichever tool they have. Keep the two
# in sync. `make help` lists targets.

.PHONY: help setup test lint format typecheck docs-lint check run

help:
	@echo "Targets: setup test lint format typecheck docs-lint check run"

# Install the dev environment (Python deps + Node lint tooling + pre-commit)
setup:
	uv sync --extra dev
	npm ci
	uv run pre-commit install

# Run the test suite (96% coverage gate, enforced in CI)
test:
	uv run pytest

# Lint (ruff check)
lint:
	uv run ruff check .

# Auto-format in place (ruff format)
format:
	uv run ruff format .

# Type-check the package
typecheck:
	uv run pyright src/clauster

# Lint docs — Markdown + YAML, the same command CI's lint job runs
docs-lint:
	bash scripts/lint-docs.sh

# All pre-PR gates: lint, format check, types, changeset lint, tests, docs, CSP guard
check:
	uv run ruff check .
	uv run ruff format --check .
	uv run pyright src/clauster
	uv run python scripts/lint_changesets.py
	uv run pytest
	bash scripts/lint-docs.sh
	node scripts/check_csp_expressions.mjs

# Run the dev server against ./clauster.yml
run:
	uv run clauster run -c clauster.yml
