# Operations

A monitoring and troubleshooting runbook for the operator running Clauster as a
service. It assembles the building blocks Clauster already ships — `/healthz`,
`/metrics`, `clauster doctor`, crash notifications, and the bridge debug log —
into one page, plus the two operational caveats that bite most often (the
`systemctl restart` / `KillMode` interaction and recovering from a corrupted
state file).

For installing Clauster as a service see [Installation](installation.md); for
the full config surface see [Configuration](reference/config.md); for the
maintenance subcommands (`config reconcile`, `keepers`, `deps`, and the rest of
the CLI) see [CLI commands](reference/cli.md).

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
| `claude_login_ok` | Whether the `claude` account is logged in (`claude auth status`; OAuth / apiKeyHelper / API key / env token all count). `true` on a cold start before the first probe, so it never cries wolf. |
| `claude_login_method` | How it is authenticated (`null` until probed). |
| `claude_login_expires_at` | Login expiry as a Unix-epoch **millisecond** timestamp, or `null` if not applicable/unknown — alert as it approaches. |
| `instances_running` | Count of bridges Clauster currently considers running. |
| `claustrum` | Present only when the hosted channel is enabled — the daemon's health (`{enabled, running, …}`). |

When auth is **on** and the caller is **unauthenticated**, the body is just
`{"status": "ok"}` — Clauster deliberately does not leak the `claude` version or
running-bridge count on a public reverse-proxy deploy.

A simple container/systemd health probe only needs the `200` + `status: ok`; a
monitoring system with credentials can additionally alert on `claude_login_ok:
false` (logged out / credentials expired — the classic "bridge runs but is dead"
mode) or an approaching `claude_login_expires_at`, on `claude_ok: false` (the
bridge host has lost its `claude` CLI), or watch `instances_running`.

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
| `node-toolchain` | On an nvm host, whether nvm's default `node` is reachable on a spawned bridge's `PATH`. | **WARN** if missing or not on the bridge `PATH` — advisory; only present on POSIX nvm hosts. |
| `port` | (CLI only) whether the listen port is free to bind. | **WARN** if already in use. |
| `systemd` | The loaded `clauster.service` uses a non-reaping `KillMode` (see below). | **WARN** if it would reap live pty bridges. |
| `extra:*` | Each optional [extra](installation.md) (`pty`/`notify`) is importable in the running interpreter. | **WARN** if missing (with the install hint) — never FAIL; a missing extra only leaves its feature dormant. A Windows-only extra is skipped off-Windows. |

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
auth guard** like every other route — so by default a scraper must satisfy the
deployment's auth, or scrape over loopback where no auth is enforced.
See [Configuration → `observability`](reference/config.md#observability-read-only-metrics-endpoint-observabilityconfig).

The endpoint exposes point-in-time gauges (and one counter) derived from live
runner state:

| Metric | Type | Meaning |
| --- | --- | --- |
| `clauster_build_info{version="…"}` | gauge | Always `1`; carries the running version as a label. |
| `clauster_bridges{status="…"}` | gauge | Number of managed bridges per lifecycle status (`starting`, `running`, `stopped`, `crashed`, `error`). |
| `clauster_projects` | gauge | Number of discovered projects. |
| `clauster_bridge_crashes_total{project="…"}` | counter | Per-project bridge crashes since process start. Only emitted once a bridge has crashed — absent (not `0`) on a process with no crashes yet, so guard `absent()` rules accordingly. |
| `clauster_bridge_cpu_percent{project="…"}` | gauge | Per-bridge process-tree CPU percent. Emitted only when the `metrics` sampler is enabled and has a fresh sample; otherwise absent. |
| `clauster_bridge_rss_bytes{project="…"}` | gauge | Per-bridge process-tree resident memory. Sampler-gated, as above. |
| `clauster_hosted_sessions` | gauge | Live hosted (claustrum) sessions. Emitted only when `claustrum.enabled`; absent otherwise. |
| `clauster_claustrum_up` | gauge | `1` if the claustrum daemon is connected, else `0`. Emitted only when `claustrum.enabled`; absent otherwise. |

### Scrape token — let Prometheus in without a session

A scraper like Prometheus can't log in through the password form. On a guarded
deployment, set a **scrape token** so the scraper can reach `/metrics` directly.
Mint one with `clauster hash-metrics-token` — it prints the raw token once (give
it to the scraper) and the hash to store at rest. (For the reverse-proxy-specific
setup, see [Networking → Scraping `/metrics`](networking.md#scraping-metrics-from-behind-the-auth-gate).)

```yaml
observability:
  prometheus_enabled: true
  metrics_token_hash: "<sha-256 hash from clauster hash-metrics-token>"  # or via *_FILE, below
```

The scraper then presents the raw token as a bearer token:

```sh
curl -s -H "Authorization: Bearer <token>" https://clauster.example.com/metrics
```

```yaml
# prometheus.yml
scrape_configs:
  - job_name: clauster
    scheme: https
    authorization:
      type: Bearer
      credentials: "<token>"
    static_configs:
      - targets: ["clauster.example.com"]
```

When `metrics_token_hash` is set, **a valid token *or* a normal session** grants
access — and only to `/metrics`, nowhere else. Only the SHA-256 hash is stored at
rest (parity with `auth.api_token_hash`), and the presented token's hash is
compared in constant time. To keep the hash out of the config file, point
`CLAUSTER_OBSERVABILITY_METRICS_TOKEN_HASH_FILE` at a file holding it. When
`metrics_token_hash` is unset, `/metrics` stays fully behind the auth guard (so a
scraper needs a session, or you scrape over loopback).

Example scrape (authenticated, loopback):

```sh
curl -s http://127.0.0.1:7621/metrics
```

```text
# HELP clauster_build_info Build information for the running Clauster.
# TYPE clauster_build_info gauge
clauster_build_info{version="0.12.x"} 1
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
and are governed by the `metrics` config block, not `observability`. **Disk I/O is
unavailable on macOS** — `psutil` has no per-process `io_counters` there, so the API
returns `null` for `disk_read_bps` / `disk_write_bps` and the card leaves those fields
blank on macOS (CPU and memory still show). See the
[platform-support matrix](reference/platforms.md) for the full per-OS list.

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
  [Configuration → `notifications`](reference/config.md#notifications-outbound-alerts-via-apprise-notificationsconfig)).

See [Configuration → `notifications`](reference/config.md#notifications-outbound-alerts-via-apprise-notificationsconfig)
for the full field reference.

## Lifecycle webhooks

Where `notifications` push a human-readable message to a chat app, **webhooks**
deliver a machine-readable JSON `POST` to your own HTTP endpoint on a Clauster
lifecycle transition — for wiring Clauster into an automation, a queue, or your
own dashboard. They are **off by default** and need no extra dependency.

```yaml
webhooks:
  enabled: true
  urls:
    - "https://example.com/hooks/clauster"
  timeout_seconds: 10.0   # per-POST timeout (>0)
  events:
    # Bridge events: an absent key defaults to ENABLED.
    spawn: true
    ready: true
    stop: true
    crash: true
    # Extended events: an absent key defaults to DISABLED — opt in explicitly.
    bg-settled: true
    permission-needed: true
    clone-done: true
```

### Events

- **Bridge events** — `spawn` / `ready` / `stop` / `crash`, one JSON `POST` per
  transition. **Default on** (an absent key stays enabled); set a key to `false` to
  mute it.
- **Extended events** — `bg-settled` (a `claude --bg` job settled),
  `permission-needed` (a hosted session parked a tool-permission prompt — the
  highest-value "come look" signal), and `clone-done` (a clone finished). **Default
  off** — opt in per key, so a sensitive attention signal never starts egressing on
  upgrade alone. Each carries an `event_type` discriminator and a distinct shape.

Every payload is redaction-safe by construction — the raw `session_<ULID>` (a
correlatable but non-reversible `session_ref` is sent instead), clone URLs, and
permission-prompt bodies are **never** egressed. See
[Public API → Lifecycle webhooks](public-api.md#lifecycle-webhooks) for the exact
payload schema of every event.

Behaviour and caveats:

- **Fail-open and best-effort.** A slow endpoint is bounded by `timeout_seconds`
  and any error is logged and swallowed — a broken webhook never blocks or breaks
  a spawn/stop. POSTs fire off the event loop and are not awaited on a lifecycle
  path.
- **`http`/`https` URLs only.** A non-`http(s)` or malformed URL is rejected at
  startup (that target is disabled, not a failed spawn). A `4xx`/`5xx` from your
  endpoint is logged but otherwise ignored — there is **no retry**.
- **Adopted / reattached bridges don't emit `spawn`/`ready`.** Events fire for
  bridges Clauster itself spawns and manages; a bridge adopted from an external
  session, or reattached after a Clauster restart, was not spawned here — so
  `ready` means "every bridge Clauster brought to RUNNING", not "every RUNNING
  bridge".
- **Secrets in URLs are yours to protect.** A token embedded in a webhook URL is
  redacted from logs, but keep it out of any shared/committed config. The URL
  comes only from this config (an operator-trusted source), never from runtime or
  user input.

See [Configuration → `webhooks`](reference/config.md#webhooks-outbound-lifecycle-webhooks-webhooksconfig)
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
  directly when the dashboard is unavailable — but **review it and scrub session
  URLs or secrets before pasting it into a public issue**, even with redaction
  enabled (the redactor only recognizes known token shapes); the redaction you
  see in the dashboard does not protect the on-disk copy.
  The on-disk filename is
  `<label>-<timestamp>-<seq>.log`, where `<label>` is the bridge label (the project
  name by default), so glob on the label:

  ```sh
  tail -f ~/.clauster/logs/<label>-*.log
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

## The `KillMode` / `systemctl restart` caveat { #restart }

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

Restore it with `clauster restore <archive> --state-dir ~/.clauster` (the
`--state-dir` target is required — point it at your configured `state_dir`,
`~/.clauster` by default). Add `--config-out /etc/clauster/clauster.yml` to also
write the config back out, and `--force` to overwrite a non-empty target.

The database schema is migrated automatically: on every start Clauster brings
`clauster.db` to the latest [Alembic](https://alembic.sqlalchemy.org/) revision
before serving, and **refuses to start** (fail-closed) if that migration fails —
so a routine upgrade-and-restart is all an in-place schema change needs. The
separate `clauster migrate` command is a legacy helper that only upgrades an
older `state.json` to the current JSON schema; on a database-backed install (the
legacy `state.json` has already been imported and renamed `state.json.imported`)
it has no meaningful state to migrate.

### Recovering from a corrupted state database

Runtime state lives in the SQLite `clauster.db` under your `state_dir` (the live
store for bridge and hosted-session records). Its durability guard is the
fail-closed schema migration: on every start Clauster brings `clauster.db` to the
latest Alembic revision before serving, in a single transaction, and **refuses to
start** if that migration fails rather than running against a half-migrated
database (see Routine backup, above). The workspace-trust writes to
`~/.claude.json` are atomic **and** additionally keep a one-time `.bak`.

> **Legacy note.** Pre-0.12 installs kept state in a JSON `state.json`, written
> atomically (write a temp file, then `os.replace`). On a current install that
> file is imported into `clauster.db` once on first boot and renamed
> `state.json.imported` — it is never read or written again, so moving it aside
> does nothing.

If `clauster.db` is ever unreadable (disk fault, a failed migration that refuses
to start):

1. **Stop Clauster** so nothing is writing.
   - Running pty bridges survive a `KillMode=process` stop (see above), so this
     is safe to do.
2. **Restore from a backup** — `clauster restore <archive> --state-dir
   ~/.clauster` (the `--state-dir` target is required — use your configured
   `state_dir`; add `--config-out PATH` to also restore the config, `--force` to
   overwrite a non-empty target). If you have no backup, **move the bad database
   aside** and let Clauster start with empty state:

   ```sh
   mv ~/.clauster/clauster.db ~/.clauster/clauster.db.corrupt
   ```

   Clauster starts fresh and **rediscovers** still-running bridges on startup
   (it matches live processes), so a lost database is not a lost session —
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
