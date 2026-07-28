# AGENTS.md

Instructions for AI coding agents working in this repository. Human contributors
should read [CONTRIBUTING.md](CONTRIBUTING.md) — this file is the machine-facing
equivalent and repeats the parts an agent needs in-context.

> Claude Code users: see [CLAUDE.md](CLAUDE.md) for Claude-specific tooling. It
> defers to this file for everything shared.

---

## The project

Clauster is a self-hosted FastAPI + Alpine/Tabler web app that spawns and manages
`claude` sessions on a remote host, driven from the browser. Published to PyPI
(`clauster`), shipped as a signed GHCR image and Sigstore-signed GitHub Releases.

**Two bridge modes, deliberately not unified:**

- **standard** — `claude remote-control` (subcommand form), multi-session.
- **pty** — `claude --remote-control` (flag form) under a PTY keeper; single
  session with true `--continue` resume.

They have different argv and different readiness logic. Do not refactor them into
one path.

---

## Build · run · test

| Task | Command |
| --- | --- |
| Install dev env | `uv sync --extra dev` |
| Run the app | `uv run clauster run -c clauster.yml` |
| Unit tests | `uv run pytest` |
| Lint | `uv run ruff check` |
| Format | `uv run ruff format` |
| Type check | `just typecheck` |
| Docs lint | `just docs-lint` |
| All local pre-PR gates | `just check` |
| Docs site build | `uv run --extra docs mkdocs build --strict` |
| Browser E2E (opt-in) | `scripts/e2e.sh` |

**Always pass `-c clauster.yml`** when running locally. A bare `uv run clauster`
falls through a three-step config search and may pick up a real config elsewhere on
the machine — including one that binds a non-loopback address.

`-n auto` (xdist) is the `addopts` default. For a **subset or custom `--cov` run**,
prepend `-o addopts=""` — the default `addopts` injects repository-wide coverage
options, so a plain subset run otherwise fails the 96% total-coverage threshold.

The browser E2E suite is excluded from the default `pytest` run and the required
CI gate; `scripts/e2e.sh` clears the addopts and runs it.

---

## Architecture

`app.py` is both the FastAPI app factory and where every route lives. The entry point is
`clauster.__main__:main`, which owns argument parsing for all subcommands plus the hidden
`__pty-keeper__` / `__recap-hook__` forms a frozen build re-invokes itself with.

**A standard spawn, end to end:** route in `app.py` → `runner.SessionRunner.spawn` →
`trust.trust_directory` + `ensure_remote_control_enabled` (both must pass — fail closed) →
subprocess → `bridge_log` parses the debug log → `logstream` tails it → `redact` →
WebSocket. Read that path before changing any part of it.

Modules under `src/clauster/`, by subsystem:

- **Bridge lifecycle** — `runner.py` (`SessionRunner`: spawn/stop/observe standard bridges) ·
  `pty_keeper.py` (sidecar owning a true-resume bridge's PTY) · `inspector.py`
  (`claude agents --json` liveness cross-check) · `supervisor.py` (`claude --bg` background
  sessions, behind `/api/agents`) · `login_shepherd.py` (dashboard-driven `claude` login).
- **Log → browser** — `bridge_log.py` · `logstream.py` · `redact.py`. Everything that
  reaches the WebSocket passes through `redact`.
- **Hosted channel** — `claustrum_client.py` (async unix-socket NDJSON JSON-RPC) ·
  `claustrum_daemon.py` (connect-or-spawn daemon lifecycle) · `hosted.py` (stream-json
  spawn, control-plane routing, redact→ring→fan-out, fail-closed permission parking) ·
  `hosted_state.py`.
- **Config · trust · auth** — `config.py` · `models.py` · `auth.py` (fails closed) ·
  `trust.py` · `discovery.py` · `provisioning.py` (create + clone) · `config_editor.py`
  (Tier-A allowlist editing) · ⚠️ `config_write.py` + `config_write_mcp.py` — the
  **code-executing** write tier; read the invariants below before touching either.
- **Ops & surfaces** — `ops.py` (`doctor`/`backup`/`restore`/`migrate`/`install-service`) ·
  `usage.py` (cost + token accounting from a transcript JSONL) · `deps.py` (optional-extras
  detection for the frozen binary) · `mcp_server.py` (`clauster mcp` stdio server) ·
  `state.py`.

**Two persistence stores, deliberately not one.** `state.json` (`state.py`) holds instances
and their bridges; hosted sessions live in `hosted_state.py` keyed by
`claustrum_process_id` so they can reattach across a clauster restart. Don't merge them.

**The UI is served as HTML, not JSON.** `templates/` renders through `jinja2_fragments`
(`Jinja2Blocks`, wired in `app.py`), so routes return Jinja *fragments* that Alpine swaps
into the page; `static/` carries the vendored Tabler + Alpine assets.

---

## Safety invariants — do not violate these

These are the project's reason-for-existing constraints. A change that breaks one is
wrong even if tests pass.

1. **Fail closed, never silently.** Auth gates default to denial. Bridge-lifecycle
   errors must surface, not collapse into a misleading state. No bare
   `except: pass`.
2. **Validate before spawning.** Resolve binaries to absolute paths and validate
   project names before any subprocess. Pass list-argv, never `shell=True`.
3. **Never widen a security boundary to make a test pass.** If a test needs auth
   off, isolate the test — don't relax the gate.
4. **Redaction is not optional.** Anything reaching the WebSocket stream goes
   through `redact.py`. Note the on-disk bridge log is verbatim unless
   `logs.redact_session_url: true` — don't describe it as redacted by default.
5. **Clauster only mutates data it owns.** Claude's own `.jsonl` transcripts are
   read-only to us.
6. **Document security options with their caveat in the same section.** A reader (or
   a retrieval system) seeing the option must see the constraint.

---

## Testing

Conventions live alongside the tests; the rules that matter most in-context:

- **`HOME` isolation is a load-bearing invariant.** `tests/conftest.py` repoints
  `HOME`/`USERPROFILE` to a throwaway dir **at conftest import time**, before any
  clauster module loads. This is deliberate: `discovery.CLAUDE_JSON`,
  `supervisor.JOBS_DIR`, and `pointers.CLAUDE_PROJECTS_DIR` resolve at *import*, so a
  function-scoped fixture structurally cannot protect them. `~/.claude.json` is a
  developer's **live** account — a test that imported and exercised `discovery` or
  `supervisor` without this pin could read or overwrite it.
  **Never remove, reorder, or run tests around that block, and never point a test at
  the real home directory.**
- Coverage is gated at **96%**. New code needs tests in the same PR.
- **Alembic is stubbed for a fresh database.** An autouse fixture swaps `upgrade_to_head`
  for a copy of a once-per-worker template, so a test that asserts on migration
  *side-effects* through `Persistence` — pre-migrate snapshots, `backups/`, Alembic call
  counts — must be marked **`@pytest.mark.real_migration`** or it exercises the copy
  instead. The failure mode is asymmetric: forgetting the marker doesn't error, it makes
  the test pass **vacuously**. `--strict-markers` catches a typo'd marker; nothing catches
  an absent one.
- Use the existing fixtures rather than constructing app state by hand.
- Tests must pass on Linux, macOS, and Windows — CI runs all three. Use
  `Path.as_posix()`, gate POSIX-only calls (`fcntl`, mode bits), and write
  byte-exact `\n`.

---

## Style

Ruff enforces style **and** docstrings (`E/F/I/W/UP/B/S/D`, pep257) at **99
columns**. Both `ruff check` and the 96% coverage gate must be green before merge.

Match the surrounding code's comment density, naming, and idiom.

---

## Git and PR workflow

**Every change goes through a branch and a PR. Never commit or push to `main`.**
The branch ruleset enforces this; CI and review are the merge gate.

- **Branch off fresh `origin/main`** — `git fetch` first. The ruleset is
  strict-up-to-date, so a stale base makes the PR show BEHIND.
- **Rebase, never merge.** History is linear and PRs are squash-merged. Fixing a
  stale branch by merging `main` into it will be rejected — `git rebase origin/main`
  instead.
- **PRs always target `main`.** Don't stack a PR on another feature branch.
- **Merge several open PRs in ascending number** (foundation first), rebasing each
  onto the new `main` after a merge.

### What gates a merge

| Gate | Detail |
| --- | --- |
| CI, all three OSes | Linux, macOS, Windows |
| Coverage | ≥96% total (pytest `--cov-fail-under`). Codecov additionally flags patch coverage below 95% on changed lines — an advisory red X, not merge-blocking, but fix uncovered new lines rather than merging past it. |
| `ruff check` + `ruff format` | 99 cols, docstrings required |
| Type check + docs lint | `just check` runs everything locally |
| **Changeset** | Add one under `.changeset/`, or apply the `no-changelog` label if the PR genuinely has no user-facing effect (CI, refactor). Keep the body to **one tight line**. Use `major` for anything breaking — including a removed or renamed config key. |
| **Code review** | [Greptile](https://www.greptile.com/) reviews every PR automatically. Its threads must be resolved before merge; unresolved threads block. Reply *and* resolve on the thread itself. ⚠️ Its free OSS tier caps at **100 reviews per billing period**, and when exhausted it posts a notice *in place of* a review — a `COMMENTED` review carrying **zero inline comments**. Check for inline comments, not for "Greptile reviewed". The maintainer can request a second opinion with `@claude review` (maintainer-only). ⚠️ "Advisory" refers to its review **state** — it submits as `COMMENT`, never `REQUEST_CHANGES`, so it cannot demand changes. Its **inline threads still block the merge** like any other: the ruleset sets `required_review_thread_resolution`, which is author-agnostic. Reply *and* resolve each one, exactly as for Greptile. |
| Docs | If the change alters behavior, update `README.md`, `docs/`, and `clauster.yml.example` **in the same PR**. The published site gates on `uv run --extra docs mkdocs build --strict`, which `just check` now runs (also available alone as `just docs-build`). It catches what `lint-docs.sh` cannot: markdownlint checks Markdown *style*, not whether a link target resolves, so a link leaving the `docs/` tree (e.g. `../UPGRADING.md` — link the rendered `upgrading.md` instead) lints clean and still fails CI. |

`Closes #N` goes in the PR **description**, never in a squash-merge commit body.

---

## Gotchas

- **The two bridge modes are not interchangeable** — different argv, different
  readiness logic. Don't unify them.
- **Releases are cut as drafts and become immutable on publish.** Don't touch the
  release path casually.
- **Config docs are generated** from the pydantic models (`<!-- BEGIN GEN -->`
  markers). Edit the model's `description=`, not the table.
- **Windows:** `shutil.which` resolves a `.cmd` but `CreateProcess` appends `.exe` —
  resolve, then exec the resolved path.
- Under systemd's *default* control-group reaping, `systemctl restart` kills live pty
  bridges. Deployments that need bridges to survive set `KillMode=process` (the
  `install-service` default).

---

## Scope discipline

Deliver what was asked. If you believe the request is mistaken or a better approach
exists, say so rather than silently widening scope. Stop short of irreversible or
destructive actions — and never commit to `main`, publish a release, or push to a
remote environment without explicit instruction.
