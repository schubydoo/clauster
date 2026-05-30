#!/usr/bin/env bash
# Build a standalone `clauster` binary with PyInstaller.
#
# Run in CI or a full environment — NOT the dev sandbox (it lacks the runtime libs
# PyInstaller-bundled apps need). Produces dist/clauster.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v pyinstaller >/dev/null 2>&1; then
  echo "pyinstaller not found — install the build extra:  uv pip install -e '.[package]'" >&2
  exit 1
fi

rm -rf build dist
pyinstaller clauster.spec
echo "Built: dist/clauster"
dist/clauster --version
