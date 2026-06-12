# Architecture

Clauster is a FastAPI app whose app factory lives in `app.py`; the entry point is
`clauster.__main__:main` (`clauster run`). It renders an Alpine.js + Jinja2 +
Tabler UI from `templates/` (with `jinja2-fragments`) and `static/`.

## Module map

Key modules under `src/clauster/`:

| Module | Responsibility |
| --- | --- |
| `app.py` | FastAPI app factory; routes, middleware, cookie/session/WS wiring. |
| `__main__.py` | CLI entry point and subcommands (`run`, `hash-password`, `doctor`, `backup`/`restore`/`migrate`, `install-service`, `reap-environments`, `usage`). |
| `runner.py` | `SessionRunner` — spawn / stop / observe **standard** `claude remote-control` bridges. |
| `pty_keeper.py` | Sidecar that owns a true-resume (**pty**) bridge's PTY. |
| `discovery.py` | Project discovery under `projects_root`; `~/.claude.json` paths. |
| `provisioning.py` | Project create + clone (with the clone/SSRF guards). |
| `trust.py` | The workspace-trust writer (atomic + flock-guarded `~/.claude.json`). |
| `bridge_log.py` | Parse the bridge debug log. |
| `logstream.py` | Tail the bridge debug log for the WebSocket stream. |
| `redact.py` | ANSI-strip + ID/secret redaction for the WS stream. |
| `inspector.py` | `claude agents --json` cross-check — the liveness source. |
| `auth.py` | Auth foundation (fail-closed; pure functions, no FastAPI import). |
| `config.py` | Config load, env-override, and validation (`ClausterConfig`). |
| `state.py` | `state.json` persistence. |
| `models.py` | Domain models. |
| `metrics.py` | Per-bridge resource sampling (CPU / memory / disk). |
| `usage.py` | Token + approximate-cost rollup from session transcripts. |
| `environments.py` | Server-side bridge-environment listing + reaper logic. |
| `supervisor.py` | Read / dispatch / stop Claude Code agent-view background sessions (`claude --bg`); backs the background-agents panel + `/api/agents`. |
| `claustrum_client.py` | Async unix-socket NDJSON JSON-RPC client for the claustrum daemon (hosted live-view channel; experimental). |
| `claustrum_daemon.py` | Connect-or-spawn lifecycle + auth-token management + health for the per-deployment claustrum daemon (experimental). |
| `hosted.py` | Hosted-channel session engine — `HostedSession` (stream-json spawn, stdout control-plane routing, redaction + ring buffer + fan-out, fail-closed permission parking) and `HostedManager` (registry, separate from the bridge runner). Has a live-view + permissions UI and reattaches across restarts; experimental. |
| `hosted_state.py` | `HostedStateStore` — persists hosted sessions to a separate `hosted_state.json` keyed by `claustrum_process_id` (the bridge `state.json` is project-keyed), so a Clauster restart can reattach them. |
| `hooks/resume_recap.py` | The `SessionStart` hook that recaps the prior conversation into a restarted bridge. |

## The two bridge modes

A bridge is a `claude` process Clauster launches in a project directory. The two
modes have **different argv and different readiness logic** and are deliberately
not unified.

> Orthogonal to these modes is an experimental **hosted channel** (`hosted.py`):
> a headless stream-json `claude` run on the claustrum daemon's pipes rather than
> as a remote-control bridge. It is a separate `channel` axis on the instance
> model, not a third bridge mode. It has its own dashboard panel — start a
> session, watch it stream live, drive it, approve/deny tool prompts, and resume
> it after a restart — backed by `WS /ws/hosted/{id}` and `GET /api/hosted`.

### standard (`claude remote-control`)

The default. `runner.py`'s `SessionRunner` spawns the headless
`claude remote-control` subcommand server:

- **Multi-session** — multiple Claude sessions per bridge.
- Survives a Clauster restart, but has **no conversation resume** — a restart
  spawns a fresh, empty context window. For continuity, the opt-in
  `claude.resume_recap` SessionStart hook recaps the most recent prior transcript
  into the new session.
- Readiness is gated on the bridge registering an environment within
  `claude.startup_grace_seconds`. A bridge that launches but can't authenticate
  to the controller stays alive yet never becomes connectable — **liveness alone
  is not "running"**. `inspector.py` cross-checks `claude agents --json` as the
  liveness source.

### pty (`claude --remote-control` under a keeper)

Opt-in via `claude.resume_mode: pty`, POSIX only (falls back to standard on
Windows). `pty_keeper.py` runs the `claude --remote-control` **flag form** under
a PTY keeper sidecar:

- **Single-session.**
- **Genuinely restores prior conversation context** on Resume (`--continue` true
  resume) — it restores rather than recaps.
- The keeper **owns the PTY** and outlives a Clauster restart; it is stopped by
  signal.

The mode is recorded on a bridge's instance **at launch** — `claude.resume_mode`
seeds new bridges only and never re-modes a running or stopped one. Stop and
resume always honour the recorded mode.

!!! note "pty readiness"
    Newer `claude` flag-form builds stopped printing the
    `claude.ai/code/session_…` connect URL, so pty readiness/ownership is gated
    on liveness rather than on parsing that line.

## Bridge lifecycle

1. **Spawn.** The `claude` binary is resolved to an absolute path and the
    project name is validated before any subprocess; argv is always a list
    (never `shell=True`). Before the first spawn Clauster acknowledges remote
    control in `~/.claude.json` (`auto_enable_remote_control`) and, if the
    directory is untrusted, the workspace-trust writer sets
    `hasTrustDialogAccepted` first.
2. **Readiness.** The bridge must register an environment within
    `startup_grace_seconds`; otherwise it is marked `ERROR`. Liveness is
    cross-checked against `claude agents --json`.
3. **Observe.** The debug log is tailed (`logstream.py`), sanitized
    (`redact.py`), and streamed over a WebSocket. Live CPU/memory/disk metrics
    are sampled from the process tree (`metrics.py`) while the bridge runs.
4. **Stop / Resume.** Stop signals the bridge. Resume relaunches it honouring its
    recorded mode — standard re-spawns (optionally recapping), pty resumes the
    keeper with `--continue`.

!!! warning "pty bridges and `systemctl restart`"
    With `KillMode=control-group`, a `systemctl restart` reaps the whole cgroup,
    which kills live pty keepers — **pty bridges do not survive a service
    restart**. A lost session's transcript is still recoverable with
    `claude --continue`.

## Configuration & state

- `config.py` loads `clauster.yml` (search order + `CLAUSTER_<UPPER_SNAKE_PATH>`
    env overrides), applies the fail-closed validators, and produces a validated
    `ClausterConfig`. See [Configuration](configuration.md).
- `state.py` persists runtime bridge state to `state.json` in the `state_dir`;
    `clauster migrate` upgrades it to the current schema, and
    `clauster backup`/`restore` tar the `state_dir` + config.

## Conventions

- **Fail closed, never silently.** Auth gates default to denial; bridge-lifecycle
    errors surface rather than collapse into a misleading state. No bare
    `except: pass` swallows.
- **Validate before spawning.** Resolve binaries to absolute paths and validate
    project names before any subprocess; pass list-argv, never `shell=True`.
- Style + docstrings enforced by ruff (`E/F/I/W/UP/B/S/D`, 99 cols); the test
    suite gates coverage at 96%.
