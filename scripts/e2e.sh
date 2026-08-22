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
# ambient global install: the CLI version is load-bearing for this suite (see
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

# Install a browser only when the caller hasn't pinned one. CI pins a Renovate-tracked
# Chrome-for-Testing build and exports AGENT_BROWSER_EXECUTABLE_PATH (see
# .github/workflows/e2e.yml, #947), so it skips this latest-Chromium download; a plain
# local run still gets a browser. Idempotent — downloads into agent-browser's cache on
# first run.
if [ -z "${AGENT_BROWSER_EXECUTABLE_PATH:-}" ]; then
  agent-browser install
fi

# --reruns: the leg runs on a 2-core CI runner that starves agent-browser, so a
# fill/click can stochastically not register (#947) and a downstream wait times out —
# a different few tests each run, cleared by a fresh re-run. Re-run a failed test up to
# three times (fresh server + browser each time) before reporting red — the flakiest
# interaction (opening a launch/menu popover) can miss a couple of consecutive attempts
# under contention. This leg opts in here; the
# required suite never reruns, so a real regression there is never masked. `"$@"` comes
# last so a caller can override (e.g. --reruns 0) or add -k.
exec uv run pytest tests/e2e -o addopts="" -m e2e --reruns 3 --reruns-delay 3 "$@"
