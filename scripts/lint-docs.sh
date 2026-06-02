#!/usr/bin/env bash
# Lint Markdown and YAML. Single source of truth for both local pre-push checks
# and the CI "ruff + pyright" job, so versions and globs never drift between them.
#
#   markdownlint-cli2 — Node tool, pinned via npx (this repo has no package.json).
#   yamllint          — Python tool, installed as a dev dependency (uv run).
#
# Usage: bash scripts/lint-docs.sh
set -euo pipefail

# Pin the markdownlint-cli2 version here (the only place it lives). Bump
# deliberately; npx fetches this exact version.
MARKDOWNLINT_CLI2_VERSION="0.22.1"

cd "$(dirname "$0")/.."

echo "==> markdownlint-cli2@${MARKDOWNLINT_CLI2_VERSION}"
npx --yes "markdownlint-cli2@${MARKDOWNLINT_CLI2_VERSION}"

echo "==> yamllint"
# No --strict: error-level problems (bad indent, duplicate keys, syntax) fail the
# build; warning-level rules (e.g. our relaxed line-length) report but don't.
# yamllint reads .yamllint.yaml at the repo root.
uv run yamllint .

echo "==> docs lint OK"
