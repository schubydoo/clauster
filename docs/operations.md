# Operations

A monitoring and troubleshooting runbook for the operator running Clauster as a
service. It assembles the building blocks Clauster already ships — `/healthz`,
`/metrics`, `clauster doctor`, crash notifications, and the bridge debug log —
into one page, plus the two operational caveats that bite most often (the
`systemctl restart` / `KillMode` interaction and recovering from a corrupted
state file).

For installing Clauster as a service see [Installation](installation.md); for
the full config surface see [Configuration](configuration.md).

## Health checks

### `/healthz` — liveness and readiness

Clauster exposes a JSON health endpoint at `/healthz`. It is the one route that
is reachable **without** authentication (so an upstream load balancer or
`systemd`/container health probe can hit it), but it only returns liveness to an
unauthenticated caller when auth is enabled:

```sh
curl -s http://127.0.0.1:7621/healthz
```

When auth is **off** (loopback) or the caller is **authenticated**, the full
body is returned:

| Field | Meaning |
| --- | --- |
| `status` | Always `"ok"` if the process is serving. |
| `version` | Clauster's own version. |
| `claude_ok` | Whether the `claude` binary probe (`claude --version`) succeeded. |
| `claude_version` | The detected `claude` CLI version (`null` if the probe failed). |
| `instances_running` | Count of bridges Clauster currently considers running. |
| `claustrum` | Present only when the hosted channel is enabled — the daemon's health (`{enabled, running, …}`). |

When auth is **on** and the caller is **unauthenticated**, the body is just
`{"status": "ok"}` — Clauster deliberately does not leak the `claude` version or
running-bridge count on a public reverse-proxy deploy.

A simple container/systemd health probe only needs the `200` + `status: ok`; a
monitoring system with credentials can additionally alert on `claude_ok: false`
(the bridge host has lost its `claude` CLI) or watch `instances_running`.

### `clauster doctor` — configuration and environment diagnostics

`clauster doctor` runs the same pre-launch diagnostics the dashboard's preflight
panel uses, from the CLI:

```sh
clauster doctor -c /etc/clauster/clauster.yml
```

It prints one line per check and exits non-zero if any check **fails**. Checks:

| Check | What it verifies | Failure vs warning |
| --- | --- | --- |
| `config` | `clauster.yml` loads and passes the fail-closed validators. | **FAIL** if missing/invalid. |
| `claude` | The `claude` binary is present and `>= claude.min_version`. | **FAIL** if absent, too old, or the probe errors. |
| `claude-login` | The runtime user's `claude` CLI has usable credentials (a spawned bridge inherits the operator's login). | **WARN** — `ANTHROPIC_API_KEY` is a valid alternative; a missing/expired token is recoverable with `claude`. |
| `projects_root` | `projects_root` exists and is a directory. | **FAIL** if not. |
| `state_dir` | `state_dir` is writable (or creatable under an existing ancestor). | **FAIL** if not writable. |
| `git` | `git` is on `PATH` (needed for `create --git-init` and clone). | **WARN** if absent. |
| `auth` | Auth is internally consistent and enforced for the bind (the same rule that refuses to start). | **FAIL** for a non-loopback bind without enforced auth. |
| `workspace-trust` | Whether `projects_root` has accepted Claude's workspace-trust dialog. | **WARN** if untrusted — advisory, recoverable from the UI (trust-on-start). |
| `version` | For a from-source checkout, whether `HEAD` is behind its last-fetched upstream. | **WARN** if behind; absent for PyPI/Docker installs. |
| `port` | (CLI only) whether the listen port is free to bind. | **WARN** if already in use. |
| `systemd` | The loaded `clauster.service` uses a non-reaping `KillMode` (see below). | **WARN** if it would reap live pty bridges. |

`claude-login` deserves a callout: it is the cause of the classic "bridge runs
but is dead" failure mode — the bridge process starts, but the inherited
`claude` login is logged out, so it can never authenticate. If a freshly spawned
bridge never becomes ready, check this first.

## Metrics

### `/metrics` — Prometheus exposition

Clauster can expose a small text-format Prometheus endpoint at `/metrics`. It is
**off by default**; enable it with:

```yaml
observability:
  prometheus_enabled: true
```

When disabled, `/metrics` returns `404`. When enabled, it stays **behind the
auth guard** like every other route — a scraper on a guarded deploy must satisfy
auth (e.g. a bearer-token / basic-auth scrape config, or scrape over loopback).
See [Configuration → `observability`](configuration.md#observability-read-only-metrics-endpoint-observabilityconfig).

The endpoint exposes a handful of point-in-time gauges derived from live runner
state:

| Metric | Type | Meaning |
| --- | --- | --- |
| `clauster_build_info{version="…"}` | gauge | Always `1`; carries the running version as a label. |
| `clauster_bridges{status="…"}` | gauge | Number of managed bridges per lifecycle status (`starting`, `running`, `stopped`, `crashed`, `error`). |
| `clauster_projects` | gauge | Number of discovered projects. |

Example scrape (authenticated, loopback):

```sh
curl -s http://127.0.0.1:7621/metrics
```

```text
# HELP clauster_build_info Build information for the running Clauster.
# TYPE clauster_build_info gauge
clauster_build_info{version="0.11.0"} 1
# HELP clauster_bridges Number of managed bridges by lifecycle status.
# TYPE clauster_bridges gauge
clauster_bridges{status="running"} 2
clauster_bridges{status="crashed"} 0
# HELP clauster_projects Number of discovered projects.
# TYPE clauster_projects gauge
clauster_projects 7
```

A useful alert is a non-zero `clauster_bridges{status="crashed"}` or `="error"`
sustained over a scrape interval.

The per-project live resource metrics (CPU / memory / disk shown on a running
bridge's card) are a separate, dashboard-only fetch
(`/api/projects/{name}/metrics`); they are not part of the Prometheus exposition
and are governed by the `metrics` config block, not `observability`.

## Crash alerts

Clauster can send an outbound notification when a bridge **crashes** — exits
unexpectedly rather than via the Stop button. Notifications go through
[Apprise](https://github.com/caronc/apprise), so any Apprise URL works (Slack,
Discord, Telegram, email, …).

They are **off by default** and require the optional `notify` extra:

```sh
pip install 'clauster[notify]'
```

```yaml
notifications:
  enabled: true
  urls:
    - "slack://TOKEN_A/TOKEN_B/TOKEN_C"
    - "tgram://bottoken/ChatID"
  notify_on_crash: true   # default; the alert that matters most for monitoring
```

Behaviour and caveats:

- **Fail-closed and best-effort.** A notification failure never affects the
  bridge lifecycle, and sends run off the event loop. If `notifications.enabled`
  is true but Apprise isn't installed, Clauster logs a warning at startup and
  sends nothing — it does not crash.
- **A crash alert means status `crashed`**, i.e. the bridge exited on its own.
  A deliberate Stop does not notify.
- **Secrets in URLs are yours to protect.** An Apprise URL often embeds a token;
  keep it out of any shared/committed config (see
  [Configuration → `notifications`](configuration.md#notifications-outbound-alerts-via-apprise-notificationsconfig)).

See [Configuration → `notifications`](configuration.md#notifications-outbound-alerts-via-apprise-notificationsconfig)
for the full field reference.

## Reading the bridge debug log

When a bridge misbehaves, the bridge's own debug log is the source of truth for
*why*. Each bridge writes a `--debug-file` debug log under the `logs/`
subdirectory of your `state_dir` (default `~/.clauster/logs/`). Clauster parses
this file for readiness and the deep link, and streams a sanitized tail of it
over a WebSocket to the dashboard's live log view.

- **From the dashboard** — open the project card's live log tail. It is
  ANSI-stripped and has session IDs redacted (`logs.strip_ansi_in_stream`,
  `logs.redact_session_url`); this is the everyday path.
- **On disk** — the public log under `<state_dir>/logs/` is, by default, the
  verbatim debug file (redaction happens only over the WebSocket unless
  `logs.redact_session_url: true`, which also redacts the on-disk copy). Tail it
  directly when the dashboard is unavailable:

  ```sh
  tail -f ~/.clauster/logs/<project>*.log
  ```

- **For a Crashed bridge** — the bridge logs its failure reason to its debug file
  before exiting, so a `crashed` card's log tail (or the on-disk file) usually
  shows the cause. A spawn that fails outright also captures a tail of the
  bridge's stdout/stderr so the UI can show *why* instead of a bare "Failed to
  start".

Bridge lifecycle states you will see on a card or in `clauster_bridges`:

| Status | Meaning |
| --- | --- |
| `starting` | Spawned; waiting to register an environment within `startup_grace_seconds`. |
| `running` | Live and ready. |
| `stopped` | Stopped via the Stop button (resumable). |
| `crashed` | Exited unexpectedly (not via Stop) — read the debug log. |
| `error` | Failed to become ready (e.g. didn't register in time, or the spawn errored). |

A bridge stuck in `starting` → `error` most often means a `claude-login` problem
(see `clauster doctor` above) or that the bridge couldn't register within
`startup_grace_seconds`.

## The `KillMode` / `systemctl restart` caveat

This is the single operational gotcha most likely to surprise you.

Clauster's spawned bridges run **inside the service's cgroup**. With systemd's
default `KillMode=control-group`, a `systemctl restart` (or `stop`) reaps the
**whole cgroup** — taking every running bridge down with the service, including
`pty` true-resume sessions, even though Clauster's own shutdown leaves them
running and would reattach them on the next start.

The unit generated by `clauster install-service systemd` sets
**`KillMode=process`** so systemd signals only the Clauster process; detached
bridges keep running and Clauster reattaches them on startup. A deliberate
`stop` then leaves bridges running (orphaned until the next start re-adopts
them) — intentional, so an upgrade restart doesn't drop live coding sessions.

- **`clauster doctor` warns** (`systemd` check) when the loaded `clauster.service`
  still uses a reaping `KillMode`.
- **To fix an older unit**: regenerate and reload —

  ```sh
  sudo clauster install-service systemd --write
  sudo systemctl daemon-reload
  sudo systemctl restart clauster.service
  ```

  That one restart still reaps the current pty bridges, but later restarts
  won't.
- **A bridge truly lost** to a crash, reboot, or that one reaping restart is
  still recoverable: its transcript persists locally, so `claude --continue` in
  the project directory resumes the conversation.

`standard`-mode bridges don't restore a conversation on restart regardless; the
`KillMode` concern is specifically about not killing live `pty` sessions. See
[Architecture → bridge lifecycle](architecture.md) and
[Installation → systemd](installation.md#run-as-a-systemd-service-linux).

## Backup, restore, and corruption recovery

### Routine backup

`clauster backup` tars the whole `state_dir` — the `clauster.db` persistence
database (the live store for bridge and hosted-session records) plus everything
else under it — together with the active config into a single archive:

```sh
clauster backup -c /etc/clauster/clauster.yml -o /var/backups/
```

Restore it with `clauster restore <archive>` (it can restore state alone or also
write the config back out).

The database schema is migrated automatically: on every start Clauster brings
`clauster.db` to the latest [Alembic](https://alembic.sqlalchemy.org/) revision
before serving, and **refuses to start** (fail-closed) if that migration fails —
so a routine upgrade-and-restart is all an in-place schema change needs. The
separate `clauster migrate` command is a legacy helper that only upgrades an
older `state.json` to the current JSON schema; on a database-backed install
(`state.json` already imported) there is nothing for it to do.

### `clauster keepers` — stop an orphaned pty keeper

A **pty** (true-resume) bridge runs under a detached *keeper* process that
outlives a Clauster restart. The normal stop path cleans up a keeper still
attached to a project card, but if the card is gone — its project was removed —
no dashboard row can show or stop it, leaving a live keeper (and its bridge)
running invisibly.

`clauster keepers` sweeps the keeper sidecars and surfaces those **orphans** (a
live keeper whose sidecar belongs to no current card):

```sh
clauster keepers -c /etc/clauster/clauster.yml             # list orphaned keepers
clauster keepers -c /etc/clauster/clauster.yml --kill 12345 # stop one by keeper PID
```

`--kill` refuses any PID that isn't a current orphan, so it can never take down a
keeper still attached to a card. On success it stops the keeper (and its bridge
subtree) and removes the stale sidecar.

### Recovering from a corrupted or partially-written state file

Clauster writes its on-disk state safely. `state.json` is written
**atomically** (write a temp file, then `os.replace`); the workspace-trust
writes to `~/.claude.json` are atomic **and** additionally keep a one-time
`.bak`. Either way a reader never sees a half-written file and an interrupted
write can't truncate the live one — a power loss or `OOM`-kill mid-write leaves
the **previous** intact file in place, not a corrupt one. (The atomic write is
the corruption guard; the `.bak` on `~/.claude.json` is a separate convenience,
and `state.json` keeps no routine backup of its own.)

If `state.json` is ever unreadable anyway (manual edit, disk fault):

1. **Stop Clauster** so nothing is writing.
   - Running pty bridges survive a `KillMode=process` stop (see above), so this
     is safe to do.
2. **Restore from a backup** — `clauster restore <archive>` — or, if you have no
   backup, **move the bad file aside** and let Clauster start with empty state:

   ```sh
   mv ~/.clauster/state.json ~/.clauster/state.json.corrupt
   ```

   Clauster starts fresh and **rediscovers** still-running bridges on startup
   (it matches live processes), so a lost `state.json` is not a lost session —
   you primarily lose recorded metadata, not the bridges themselves.
3. **For `~/.claude.json`** (workspace trust / remote-control acknowledgement):
   the trust writer keeps a one-time `~/.claude.json.bak` taken before its first
   modification. If that file is damaged, the `.bak` is the recovery source.
   Because the `claude` CLI writes the same file, prefer letting `claude` rewrite
   it (re-accept trust from the dashboard) over hand-editing.
4. **Run `clauster doctor`** to confirm `config`, `state_dir`, and `claude-login`
   are green before resuming normal operation.

Keep periodic `clauster backup` archives off-host so step 2 always has a clean
restore point.

## A quick monitoring checklist

- **Probe `/healthz`** from your load balancer / systemd / container runtime
  (unauthenticated liveness is enough for the probe).
- **Scrape `/metrics`** (enable `observability.prometheus_enabled`, authenticated
  scrape) and alert on sustained `clauster_bridges{status="crashed"}` /
  `="error"`.
- **Wire `notifications`** with `notify_on_crash` for push alerts on unexpected
  bridge exits.
- **Run `clauster doctor`** after any config change or upgrade; it catches the
  logged-out-`claude`, non-loopback-without-auth, and reaping-`KillMode` traps.
- **`clauster backup` on a schedule**, archives stored off-host.
