#!/usr/bin/env bash
# Run the opt-in browser E2E suite (Playwright + real headless Chromium).
#
# The E2E suite is excluded from the default/CI test run (`--ignore=tests/e2e` in
# pyproject) so it never adds a browser dependency or latency to the required
# `tests` gate. This script runs it on demand: it clears the default addopts (drop
# the coverage gate + xdist + the ignore), installs the browser if needed, and runs
# only tests/e2e. Pass through any extra pytest args, e.g. `scripts/e2e.sh -k login`.
set -euo pipefail
cd "$(dirname "$0")/.."

# Idempotent; downloads Chromium into Playwright's cache on first run.
uv run playwright install chromium

exec uv run pytest tests/e2e -o addopts="" -m e2e "$@"
