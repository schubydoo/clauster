#!/usr/bin/env bash
# Lint Markdown and YAML, and verify the generated config reference is in sync.
# Single entrypoint for local pre-push checks and the CI "lint" job, so versions
# and globs never drift between them.
#
#   markdownlint-cli2 — Node tool, version pinned in package.json + integrity-
#                       checked via package-lock.json (`npm ci`). Run from the
#                       local node_modules with `npx --no-install` (no network
#                       fetch, no unverified download).
#   yamllint          — Python tool, installed as a dev dependency (uv run).
#   config reference  — gen_config_reference.py --check fails if the tables in
#                       docs/configuration.md drifted from the config models. The
#                       tests job covers config.py changes; running it here also
#                       catches a hand-edit of the generated tables on a docs-only
#                       PR (where the tests job is skipped).
#
# Prereqs: `npm ci` and `uv sync --extra dev` (see CONTRIBUTING). CI installs both.
# Usage:   bash scripts/lint-docs.sh
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> markdownlint-cli2 (node_modules; version pinned in package.json)"
npx --no-install markdownlint-cli2

echo "==> yamllint"
# No --strict: error-level problems (bad indent, duplicate keys, syntax) fail the
# build; warning-level rules (e.g. our relaxed line-length) report but don't.
# yamllint reads .yamllint.yaml at the repo root.
uv run yamllint .

echo "==> config reference (gen_config_reference.py --check)"
uv run python scripts/gen_config_reference.py --check

echo "==> docs lint OK"
