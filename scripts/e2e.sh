#!/usr/bin/env bash
# Run the opt-in browser E2E suite (agent-browser + real headless Chromium).
#
# The E2E suite is excluded from the default/CI test run (`--ignore=tests/e2e` in
# pyproject) so it never adds a browser dependency or latency to the required
# `tests` gate. This script runs it on demand: it clears the default addopts (drop
# the coverage gate + xdist + the ignore), installs the browser if needed, and runs
# only tests/e2e. Pass through any extra pytest args, e.g. `scripts/e2e.sh -k login`.
#
# The tests drive Chromium through Vercel's `agent-browser` CLI (see
# tests/e2e/_driver.py), which must be on PATH (`npm i -g agent-browser`).
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v agent-browser >/dev/null 2>&1; then
  echo "agent-browser not found on PATH — install it with: npm i -g agent-browser" >&2
  exit 1
fi

# Idempotent; downloads the matched Chromium into agent-browser's cache on first run.
agent-browser install

exec uv run pytest tests/e2e -o addopts="" -m e2e "$@"
