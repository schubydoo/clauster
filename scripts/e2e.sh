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
# tests/e2e/_driver.py), installed version-pinned from tests/e2e/package-lock.json.
set -euo pipefail
cd "$(dirname "$0")/.."

# Prefer the version-pinned agent-browser from tests/e2e/package-lock.json over an
# ambient global install: 0.27.3 regressed `check` on the trust-gate checkbox (see
# tests/e2e/package.json), so the lockfile version is the verified-green one. CI
# preinstalls it the same way (e2e.yml: npm ci + node_modules/.bin on PATH); locally
# this installs it on demand, falling back to a global agent-browser only when npm
# itself is unavailable.
if command -v npm >/dev/null 2>&1; then
  # Always npm ci (not just when the binary is missing): an executable left by an
  # earlier checkout could be a DIFFERENT version than the lockfile now pins, which
  # would silently reintroduce exactly the version-specific failures the pin prevents.
  (cd tests/e2e && npm ci --no-audit --no-fund)
fi
if [ -x tests/e2e/node_modules/.bin/agent-browser ]; then
  export PATH="$PWD/tests/e2e/node_modules/.bin:$PATH"
fi

if ! command -v agent-browser >/dev/null 2>&1; then
  echo "agent-browser not found — install Node/npm and re-run (this script npm-ci's the pinned CLI in tests/e2e)" >&2
  exit 1
fi

# Idempotent; downloads the matched Chromium into agent-browser's cache on first run.
agent-browser install

exec uv run pytest tests/e2e -o addopts="" -m e2e "$@"
