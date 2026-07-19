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

App factory in `app.py`; entry point is `clauster.__main__:main` (`clauster run`).
Key modules under `src/clauster/`:

- **`runner.py`** — `SessionRunner`: spawn / stop / observe standard bridges.
- **`pty_keeper.py`** — sidecar owning a true-resume (pty) bridge's PTY.
- **`discovery.py` · `provisioning.py` · `trust.py`** — project discovery, create +
  clone, workspace-trust writer.
- **`bridge_log.py` · `logstream.py` · `redact.py`** — parse, tail, and redact the
  bridge debug log for the WebSocket stream.
- **`inspector.py`** — `claude agents --json` cross-check (liveness source).
- **`supervisor.py`** — read/dispatch/stop for agent-view background sessions
  (`claude --bg`); backs the background-agents panel and `/api/agents`.
- **`claustrum_client.py` · `claustrum_daemon.py`** — async unix-socket NDJSON
  JSON-RPC client, and connect-or-spawn lifecycle for the claustrum daemon.
- **`hosted.py` · `hosted_state.py`** — hosted-channel engine (stream-json spawn,
  control-plane routing, redact→ring→fan-out, fail-closed permission parking) and
  its separate persistence keyed by `claustrum_process_id`.
- **`auth.py`** — auth foundation; fails closed.
- **`config.py` · `state.py` · `models.py`** — config load, `state.json`
  persistence, domain models.

`templates/` (Jinja + jinja2-fragments) and `static/` render the Alpine/Tabler UI.

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
| **Code review** | [Greptile](https://www.greptile.com/) reviews every PR automatically. Its threads must be resolved before merge; unresolved threads block. Reply *and* resolve on the thread itself. |
| Docs | If the change alters behavior, update `README.md`, `docs/`, and `clauster.yml.example` **in the same PR**. The published site gates on `uv run --extra docs mkdocs build --strict` (not part of `just check`) — run it whenever `docs/` or `mkdocs.yml` changes. |

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
