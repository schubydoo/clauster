# Architecture

Clauster is a FastAPI app whose app factory lives in `app.py`; the entry point is
`clauster.__main__:main` (`clauster run`). It renders an Alpine.js + Jinja2 +
Tabler UI from `templates/` (with `jinja2-fragments`) and `static/`.

## Module map

Key modules under `src/clauster/`:

| Module | Responsibility |
| --- | --- |
| `app.py` | FastAPI app factory; routes, middleware, cookie/session/WS wiring. |
| `__main__.py` | CLI entry point and subcommands (`run`, `hash-password`, `hash-token`, `hash-metrics-token`, `api-token` (`issue`/`list`/`rotate`/`revoke`), `mcp`, `doctor`, `backup`/`restore`/`migrate`, `install-service`, `reap-environments`, `keepers`, `usage`, `config` (with `config reconcile`)). |
| `runner.py` | `SessionRunner` — spawn / stop / observe **standard** `claude remote-control` bridges. |
| `pty_keeper.py` | Sidecar that owns a true-resume (**pty**) bridge's PTY. |
| `discovery.py` | Project discovery under `projects_root`; `~/.claude.json` paths. |
| `provisioning.py` | Project create + clone (with the clone/SSRF guards). |
| `trust.py` | The workspace-trust writer (atomic + flock-guarded `~/.claude.json`). |
| `bridge_log.py` | Parse the bridge debug log. |
| `logstream.py` | Tail the bridge debug log for the WebSocket stream. |
| `redact.py` | ANSI-strip + ID/secret redaction for the WS stream. |
| `inspector.py` | `claude agents --json` cross-check — the liveness source. |
| `procutil.py` | `psutil`-based process introspection: liveness with PID-reuse defense (create-time + cmdline match) and the match-gated kill behind bridge rediscovery and hosted orphan recovery. |
| `auth.py` | Auth foundation (fail-closed; pure functions, no FastAPI import). |
| `config.py` | Config load, env-override, and validation (`ClausterConfig`). |
| `config_editor.py` · `config_write*.py` | Tier-A config-editor write backends — read/validate/write the runtime `claude` config surfaces (settings, permissions, hooks, MCP, plugins, skills, subagents) from the dashboard, behind the `config_write` gates. |
| `login_shepherd.py` · `login_status.py` | Dashboard `claude` account login flow — subscription login (plain pipes) and long-lived `setup-token` (under a POSIX PTY or a Windows ConPTY, reusing `PtyScreen`), plus the account login-status probe surfaced in `/healthz`; behind the `login_shepherd` gates. |
| `db/` | Persistence layer: `engine.py` (resolves the SQLite `clauster.db` URL under `state_dir`), `models.py`/`stores.py` (SQLAlchemy schema + record stores), `bootstrap.py` (startup Alembic-to-head + one-time legacy-JSON import, both fail-closed), and the packaged Alembic `migrations/`; the `stores.py` layer includes an append-only `session_events` lifecycle history (`SessionHistoryStore`) that backs the Projects last-used / cost sort. |
| `state.py` | Legacy `state.json` store — now an import source only (the live store is `clauster.db`). |
| `models.py` | Domain models. |
| `metrics.py` | Per-bridge resource sampling (CPU / memory / disk). |
| `usage.py` | Token + approximate-cost rollup from session transcripts. |
| `notify.py` | Outbound lifecycle notifications via Apprise (optional `notify` extra). |
| `webhooks.py` | Outbound HTTP lifecycle webhooks (opt-in extension seam) — POST on spawn/ready/stop/crash plus bg-settled/permission-needed/clone-done, fail-open, with an opt-in SSRF deny-list. |
| `prometheus.py` | Text exposition for the opt-in read-only `/metrics` endpoint. |
| `environments.py` | Server-side bridge-environment listing + reaper logic. |
| `supervisor.py` | Read / dispatch / stop Claude Code agent-view background sessions (`claude --bg`); backs the background-agents panel + `/api/agents`. |
| `claustrum_client.py` | Async unix-socket NDJSON JSON-RPC client for the claustrum daemon (hosted live-view channel; experimental). |
| `claustrum_daemon.py` | Connect-or-spawn lifecycle + auth-token management + health for the per-deployment claustrum daemon (experimental). |
| `hosted.py` | Hosted-channel session engine — `HostedSession` (stream-json spawn, stdout control-plane routing, redaction + ring buffer + fan-out, fail-closed permission parking) and `HostedManager` (registry, separate from the bridge runner). Has a live-view + permissions UI and reattaches across restarts; experimental. |
| `hosted_state.py` | Legacy `hosted_state.json` store for hosted sessions, keyed by `claustrum_process_id` — now an import source only (hosted-session records live in `clauster.db`, letting a Clauster restart reattach them). |
| `hooks/resume_recap.py` | The `SessionStart` hook that recaps the prior conversation into a restarted bridge. |

## The two bridge modes

A bridge is a `claude` process Clauster launches in a project directory. The two
modes have **different argv and different readiness logic** and are deliberately
not unified.

## Session modes (canonical vocabulary)

These four names describe how clauster spawns and talks to a `claude` process. Use
exactly these display names in UI labels, tooltips, help/offcanvas copy, and docs;
the serialized wire tokens (`standard`, `pty`, `hosted`) never change.

- **Server Mode** — a headless `claude remote-control` server (the subcommand
  form) that hosts multiple Desktop/web sessions at once and survives a clauster
  restart, but does not resume a prior conversation. (wire token: launch/resume
  mode `"standard"`)
- **Interactive Session** — a single `claude --remote-control` (flag form) bridge
  run under a PTY keeper, pairing a live terminal with remote control so it can
  genuinely restore the prior conversation on restart (true resume). (wire token:
  launch/resume mode `"pty"`)
- **Background Agent** — a fire-and-forget `claude --bg` agent-view run that
  executes a task on its own and reports back, with no attached interactive
  session. (the `--bg` / agent-view substrate)
- **Direct Session** — clauster drives `claude` itself over the claustrum-daemon
  stream-json channel and renders the conversation in its own UI, rather than
  proxying a remote-control bridge. (wire token: session channel `"hosted"`)

Reference: <https://code.claude.com/docs/en/remote-control> — Server mode is the
`claude remote-control` multi-session server; an interactive session is
`claude --remote-control`, a terminal paired with remote control.

> Orthogonal to these modes is the experimental **Direct Session** channel
> (`hosted.py`, wire token `channel` `"hosted"`): a headless stream-json `claude` run
> on the claustrum daemon's pipes rather than as a remote-control bridge. It is a
> separate `channel` axis on the instance model, not a third bridge mode. It has its
> own dashboard panel — start a session, watch it stream live, drive it, approve/deny
> tool prompts, and resume it after a restart — backed by `WS /ws/hosted/{id}` and
> `GET /api/hosted`.
>
> **The key user-visible difference:** a **bridge** (`remote-control` / pty) is
> **cloud-visible** — attachable from `claude.ai/code` and the Claude mobile app.
> A **Direct Session** is **local live-view only** — it streams in this dashboard
> but is *never* attachable from the Claude app. Starting a Direct Session and then
> trying to open it on your phone is a dead end by design.
>
> With `claustrum.keep_children` (default on) the daemon is spawned with
> `-keep-children`, so a daemon restart or upgrade leaves hosted agents running:
> on reconnect Clauster reattaches the sessions it can, and marks survivors it
> can no longer drive as recoverable **orphans** (Kill / Resume from the
> dashboard). A survivor is hard-killed only when it is provably still the same
> process — alive, recorded create-time match, hosted cmdline
> (`procutil.is_killable_hosted`); without that evidence it is reported lost,
> never killed.

### Server Mode (`claude remote-control`)

The default (Server Mode). `runner.py`'s `SessionRunner` spawns the headless
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

### Interactive Session (`claude --remote-control` under a keeper)

Opt-in via `claude.launch_mode: pty`. On POSIX it runs under a real pty; on Windows
under a ConPTY keeper (pywinpty, the `pty` extra), falling back to Server Mode when
that extra is absent — see [Platform support](#platform-support). `pty_keeper.py` runs
the `claude --remote-control` **flag form** under
a PTY keeper sidecar:

- **Single-session.**
- **Genuinely restores prior conversation context** on Resume (`--continue` true
  resume) — it restores rather than recaps.
- The keeper **owns the PTY** and outlives a Clauster restart; it is stopped by
  signal.

The mode is recorded on a bridge's instance **at launch** — `claude.launch_mode`
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

!!! note "Interactive Session bridges and `systemctl restart`"
    Interactive Session bridges survive a `systemctl restart` under the
    `KillMode=process` unit `install-service` writes by default (the keeper is
    detached and reattached on startup). They're only reaped under systemd's
    default `KillMode=control-group`, which kills the whole service cgroup —
    `clauster doctor` flags that case. A session lost to a cgroup reap still has its
    transcript recoverable with `claude --continue`.

## Configuration & state

- `config.py` loads `clauster.yml` (search order + `CLAUSTER_<UPPER_SNAKE_PATH>`
    env overrides), applies the fail-closed validators, and produces a validated
    `ClausterConfig`. See [Configuration](configuration.md).
- `db/` is the persistence layer: `db/engine.py` resolves the SQLite `clauster.db`
    URL under `state_dir` (Clauster is SQLite-only, #796) and `db/bootstrap.py`
    runs the Alembic migrations to head on startup — fail-closed, refusing to
    start on a failed migration — then performs a one-time import of any legacy
    `state.json` / `hosted_state.json` (renaming each to `*.imported`). `state.py` /
    `hosted_state.py` remain only as those legacy JSON stores and import sources.
- `clauster migrate` is a legacy helper that upgrades an older `state.json` to
    the current JSON schema (the database schema is migrated automatically at
    startup, above); `clauster backup`/`restore` tar the `state_dir`
    (`clauster.db` included) + config.

## Platform support

Clauster runs on Linux, macOS, and Windows. A handful of runtime capabilities
are POSIX-specific or otherwise platform-bound; the table below is the single
source of truth for "what works where". When one of these rows changes, update
it **here** rather than restating the gap in another doc — the scattered notes
elsewhere point back to this matrix.

Legend: ✓ works · ✗ not available (honest platform gap) · 🟡 in progress

| Capability | Linux | macOS | Windows | Notes |
| --- | :---: | :---: | :---: | --- |
| Standard (Server Mode) bridge | ✓ | ✓ | ✓ | Windows spawns the bridge with `CREATE_NEW_PROCESS_GROUP` and stops it via `CTRL_BREAK_EVENT`; POSIX uses `start_new_session` + `SIGINT`. |
| Interactive Session (PTY) bridge | ✓ | ✓ | ✓ †‡ | POSIX uses `pty.openpty` + `termios`; Windows drives a **ConPTY** keeper via **pywinpty** ([#903](https://github.com/schubydoo/clauster/pull/903)). † Needs the `pty` extra (`pip install 'clauster[pty]'`) — without it `launch_mode: pty` falls back to Server Mode. ‡ **Stopping** an Interactive Session on Windows is a hard kill: the bridge lives in the keeper's *separate* ConPTY console, so the graceful `CTRL_BREAK` can't reach it — the local process is reaped, but the cloud session isn't deregistered (it re-registers on next launch). POSIX/macOS get the orderly double-`SIGINT`. |
| Hosted channel (claustrum) | ✓ | ✓ | ✓ | Windows dials claustrum's named pipe (discovered via `rpc.pipe`) rather than the `AF_UNIX` socket, and clauster spawns the daemon with `-listen-pipe` + a `-token-file` handoff (a numeric token fd isn't usable there). Round-trip validated ([#902](https://github.com/schubydoo/clauster/pull/902)). |
| Dashboard `claude` login | ✓ | ✓ | ✓ † | Subscription sign-in (`claude auth login`, plain pipes) works everywhere; the long-lived `setup-token` flow runs under a POSIX PTY on Linux/macOS and a **ConPTY** (pywinpty) on Windows ([#905](https://github.com/schubydoo/clauster/issues/905)). Because a ConPTY echoes written input back — which a parent can't disable the way POSIX `termios` does — the operator-pasted code is registered and redacted out of any returned/logged output. † Like the Interactive Session, the Windows ConPTY path needs the `pty` extra (`pip install 'clauster[pty]'`); without it `setup-token` fails closed with a clear message (subscription sign-in still works). |
| Config-write CLI (`claude mcp` / `claude plugin`) | ✓ | ✓ | ✓ | Routes exercised on the Windows CI cells + VM. |
| Per-bridge CPU % / RSS memory | ✓ | ✓ | ✓ | `psutil.cpu_times` / `memory_info` on every platform. |
| Per-bridge disk I/O rate | ✓ | ✗ | ✓ | `psutil` has no per-process `io_counters` on macOS, so a bridge card's `disk_read_bps` / `disk_write_bps` fields are blank there. |
| Advisory file locking (config writes) | ✓ | ✓ | ✓ ‡ | An in-process lock serializes clauster's own concurrent writers on every OS; POSIX additionally takes an advisory `fcntl.flock` (which also guards *other* clauster processes). Neither coordinates with the `claude` CLI (which takes no lock) — that's the atomic `os.replace` (no torn files) + the caller's external-edit hash guard. ‡ Cross-clauster-process serialization needs the POSIX flock; a single clauster process is fully covered everywhere. |
| Owner-only file modes (`0o600` / `0o700`) | ✓ | ✓ | ✓ ‡ | POSIX sets `0700`/`0600` mode bits; Windows sets an equivalent owner-only ACL on the state dir via `icacls` (remove inheritance; grant only the current user + SYSTEM). ‡ The Windows ACL is best-effort: the default `state_dir` (under `%USERPROFILE%`) already inherits a user + SYSTEM + Administrators ACL, so if `icacls` can't run (absent, a domain/service account with no resolvable `USERNAME`, a non-zero exit) clauster logs a loud warning and proceeds on the inherited ACL rather than blocking every state write. Relocate `state_dir` outside the user profile and you should tighten it yourself. |
| Directory `fsync` (crash durability) | ✓ | ✓ | ✓ | POSIX `fsync`s the parent directory after a rename; Windows can't `fsync` a directory handle, but NTFS **journals** the metadata so the rename is recovered on reboot — equivalent durability via a different mechanism. |
| Service-unit install (`install-service`) | ✓ | ✓ | ✓ † | Renders a systemd unit on Linux, a launchd plist on macOS (`launchd` kind), and an nssm install script on Windows (`windows` kind) — `ops.render_service_unit`. † Windows requires `nssm` installed + on `PATH` before running the generated script. |

The remaining gaps above are honest platform differences, not defects. All three OS
cells enforce the same `--cov-fail-under=96` gate; Windows measures through
`.coveragerc-win`, which excludes the POSIX/ConPTY code it genuinely can't run, so each
platform holds the floor on the code it actually executes (all three currently sit at
100% on their runnable surface, and the union across platforms is 100%). The per-OS
Codecov flags add visibility on top of the gate. The **ConPTY keeper and the live pty-screen view both need
the `pty` extra** (`pip install 'clauster[pty]'`, pulling pywinpty on Windows and pyte
everywhere); it is intentionally not bundled in the standalone binary — see the module
notes and [#904](https://github.com/schubydoo/clauster/issues/904).

## Conventions

- **Fail closed, never silently.** Auth gates default to denial; bridge-lifecycle
    errors surface rather than collapse into a misleading state. No bare
    `except: pass` swallows.
- **Validate before spawning.** Resolve binaries to absolute paths and validate
    project names before any subprocess; pass list-argv, never `shell=True`.
- Style + docstrings enforced by ruff (`E/F/I/W/UP/B/S/D`, 99 cols); the test
    suite gates coverage at 96% on all three OSes (Windows measures through
    `.coveragerc-win`, which excludes the POSIX/ConPTY code it can't run).
