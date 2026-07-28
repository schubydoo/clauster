# CLAUDE.md

**The shared instructions for this repo live in [AGENTS.md](AGENTS.md): build/test
commands, architecture, safety invariants, testing rules, style, and the git/PR
workflow. The line below imports it, so it loads with this file — you do not need to
open it separately. This file adds only what is specific to Claude Code.**

@AGENTS.md

Keeping the shared rules in one file is deliberate. A second copy drifts on the next
merge, and stale instructions are worse than none.

⚠️ **That `@AGENTS.md` line is load-bearing — do not "tidy" it into a plain link.**
Claude Code reads `CLAUDE.md`, not `AGENTS.md`; a markdown link is a suggestion an agent
may or may not follow, while `@` is an import that is expanded into context at launch.
This file linked rather than imported until 2026-07-27, which meant the safety invariants
and merge gates were never actually delivered — every session either chose to open
`AGENTS.md` or worked without it. Note `@` is inert inside backticks or code fences, so
`` `@AGENTS.md` `` in prose does NOT import.

---

## Local overrides

`CLAUDE.local.md` is gitignored and loads *after* this file. Host-specific paths,
personal tooling preferences, and machine operational notes belong there — not here.
If you find yourself adding something to this file that would only be true on one
machine, it goes in `CLAUDE.local.md` instead.

`.claude/` is also gitignored: project subagents, skills, hooks, and rules are
local-only and vary per contributor. Do not assume any of them exist, and do not
reference them from committed files — a contributor without them would read a broken
pointer.

---

## Working in this repo

- **Prefer the dedicated file/search tools over shell equivalents.** Read/Edit/Grep
  rather than `cat`/`sed`/`grep`, so edits stay tracked.
- **Run `just check` before proposing a PR** — it runs the local test, coverage,
  ruff, type, docs-lint, and docs-site-build (`mkdocs build --strict`) gates. CI
  additionally runs the 3-OS matrix, package build, and platform smoke jobs.
  Cheaper to fail locally. Running one sub-gate by hand is not a substitute: a
  broken docs link passes `lint-docs.sh` and only the site build catches it.
- **Verify before you report.** A status line in a planning doc, a TODO, a changelog,
  or a recalled memory is a *cache*, not truth. Confirm open-vs-shipped against
  `git log`, `gh pr view`, or the code itself before acting on it or telling the user
  something is done. This project has repeatedly been bitten by items marked open
  that had already shipped, and vice versa.
- **Say what actually happened.** If tests fail, show the output. If a step was
  skipped, say so. Don't describe work as verified when it was only written.
- **Changing a workflow?** `just check` does not lint `.github/workflows/`. Run both:
  `uvx zizmor <file>.yml` (the security audit CI runs) and `actionlint <file>.yml`
  (schema, expressions, and shellcheck over the embedded `run:` blocks — install it
  separately, and it silently skips the shell linting if `shellcheck` isn't on PATH).
- **When delegating to a subagent**, pass the same verification requirement
  explicitly — a sweep that trusts doc prose will report stale status as fact.

## Docs changes

The docs are user-facing and drift easily from the code. When changing behavior:

- Update `README.md`, the relevant `docs/` page, and `clauster.yml.example` in the
  **same PR**.
- Config reference tables are **generated** from the pydantic models — edit the
  model's `description=`, not the rendered table.
- Prefer linking over restating. Repeating a fact in a second file creates two
  caches that will disagree after the next change.

## Security-sensitive work

`auth.py`, `trust.py`, `redact.py`, the bridge-spawn path, and anything touching
config writes carry the invariants listed in AGENTS.md. For changes there, state
plainly which invariant your change touches and how it stays satisfied — rather than
asserting the change is safe.

Never weaken a gate to make a test pass; isolate the test instead.
