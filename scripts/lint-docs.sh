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
#   private refs     — no tracked file may cite the agent memory store (a private,
#                       gitignored per-host directory). Such a pointer is dead on
#                       arrival for every contributor; see the block below.
#   config reference  — gen_config_reference.py --check fails if the tables in
#                       docs/reference/config.md or docs/guides/config-editor.md
#                       drifted from the config models. The
#                       tests job covers config.py changes; running it here also
#                       catches a hand-edit of the generated tables on a docs-only
#                       PR (where the tests job is skipped).
#
# NOT covered here: link/nav resolution. markdownlint checks Markdown *style*, not
# whether a link target exists, so a link that leaves the docs/ tree (e.g.
# `../UPGRADING.md`) passes this script and still fails CI's docs job. That gate is
# `mkdocs build --strict`, which needs the `docs` extra while this script runs in
# CI's lint job under `dev` — so it lives in `just docs-build`, and `just check`
# runs it alongside this script. Adding a docs link? Run `just check`, not just this.
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

echo "==> no references to the private agent memory store"
# Agents carry a per-host, gitignored memory store (~/.claude/projects/*/memory/)
# whose notes are named `project-*` / `reference-*` / `feedback-*` and cross-linked
# with [[wiki-links]]. Citing one from a tracked file leaves a pointer no
# contributor — and no future agent on another host — can follow. Two have shipped
# this way already (a test comment, a module docstring), so this is a real gate and
# not a hypothetical one. Put the fact in the file, or cite a PR/issue instead.
#
# tests/fixtures/ is exempt: those are captured `claude` transcripts, verbatim
# third-party payloads we must not edit to satisfy a linter.
# shellcheck disable=SC2016  # the regex is literal; backticks must NOT be substituted
private_ref_re='(\[\[[a-z0-9]+([_-][a-z0-9]+)+\]\]|`(project|reference|feedback)[_-][a-z0-9]+([_-][a-z0-9]+){2,}`)'
if hits=$(git ls-files -z ':!scripts/lint-docs.sh' ':!tests/fixtures' \
    | xargs -0 grep -nEI "$private_ref_re" 2>/dev/null); then
  echo "ERROR: tracked file(s) cite the private agent memory store:" >&2
  echo "$hits" >&2
  echo "Inline the fact, or cite a PR/issue number instead." >&2
  exit 1
fi

echo "==> docs lint OK"
