# Configuration

All settings live in `clauster.yml`. The full, commented schema is in
[`clauster.yml.example`](https://github.com/schubydoo/clauster/blob/main/clauster.yml.example).
This page is the exhaustive field reference. The tables below are **generated from
the pydantic models** in `src/clauster/config.py` (via
`scripts/gen_config_reference.py`), so they never drift from the code — a CI check
fails the build if a field is added or changed without regenerating this page.

## Loading & overrides

Clauster searches for a config in this order (first file that exists wins):

1. The path passed to `clauster run -c <path>` (explicit).
2. `$CLAUSTER_CONFIG`
3. `./clauster.yml`
4. `$CLAUSTER_HOME/clauster.yml`

If none is found, startup fails with a `FileNotFoundError` listing the paths it
searched.

**Environment overrides.** Any *scalar* key is overridable by an environment
variable named `CLAUSTER_<UPPER_SNAKE_PATH>` — the dotted path uppercased and
joined with underscores. For example:

- `auth.enabled` → `CLAUSTER_AUTH_ENABLED`
- `auth.password_hash` → `CLAUSTER_AUTH_PASSWORD_HASH`
- `claude.resume_mode` → `CLAUSTER_CLAUDE_RESUME_MODE` *(full dotted path — see note)*

!!! note "Env mapping is by leaf path"
    The mapping recurses nested models and uses the *full dotted path*, so
    `claude.resume_mode` maps to `CLAUSTER_CLAUDE_RESUME_MODE`. `dict`/`list`
    leaves (e.g. `projects`, `reverse_proxy.trusted_ips`,
    `clone.allowed_private_cidrs`) **cannot** be set via env — a single env var
    can't express them unambiguously; set those in the YAML file.

**Secret files (`*_FILE`).** Every `CLAUSTER_<X>` variable also accepts a
`CLAUSTER_<X>_FILE` form that reads the value from a file instead of the
environment — for secrets that Docker / Podman / Kubernetes / Vault render to
files under `/run/secrets` rather than env vars, keeping them out of the process
environment. The file's contents win over the plain variable, and trailing
whitespace (e.g. a trailing newline) is stripped. An unreadable `_FILE` path is a
fatal misconfiguration (it does not silently fall back). The session secret has
its own `CLAUSTER_SESSION_SECRET_FILE` (it is read outside the config schema):

- `auth.password_hash` → `CLAUSTER_AUTH_PASSWORD_HASH_FILE`
- `auth.api_token_hash` → `CLAUSTER_AUTH_API_TOKEN_HASH_FILE`
- session secret → `CLAUSTER_SESSION_SECRET_FILE`

```yaml
# docker-compose: render a secret to a file and point clauster at it
services:
  clauster:
    environment:
      CLAUSTER_AUTH_PASSWORD_HASH_FILE: /run/secrets/clauster_pw_hash
    secrets:
      - clauster_pw_hash

secrets:
  clauster_pw_hash:
    file: ./secrets/pw_hash.txt
```

**Schema is additive-only.** Old configs always validate against newer versions;
unknown per-project keys are ignored.

## Top level (`ClausterConfig`)

<!-- BEGIN GEN: clauster -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `schema_version` | int | `1` | Config schema version (additive-only). |
| `projects_root` | path | *(required)* | Directory whose children become project cards. Must exist, be a directory, and be readable — validated at load. `~` is expanded. |
| `host` | str | `127.0.0.1` | Bind address. A non-loopback host requires enforced auth (see Networking). |
| `port` | int | `7621` | Bind port (1–65535). |
| `state_dir` | path | `~/.clauster` | Where `state.json` and runtime state live. `~` is expanded. |
| `database_url` | str \| null | `null` | SQLAlchemy URL for the persistence database. Unset (the default) uses a SQLite file `clauster.db` under `state_dir`. Set a Postgres DSN (e.g. `postgresql+psycopg://…`) for a shared/multi-user deployment. |
| `root_path` | str | `""` | ASGI `root_path` for serving under a reverse-proxy sub-path. |
| `log_format` | `text` \| `json` | `text` | Application log format. `text` (default) is the human single-line format; `json` emits one structured JSON object per record. Both modes redact session URLs / bearer ids before the line is written. |
| `instance_name` | str \| null | `null` | Optional label (≤32 chars, `[A-Za-z0-9_.-]`). When set, retitles the process to `clauster[<name>]` so co-resident instances are distinguishable in `ps`/`pgrep`. Cosmetic only. |
<!-- END GEN: clauster -->

Nested sections: `claude`, `instance_defaults`, `projects`, `auth`, `logs`,
`clone`, `reaper`, `usage`, `metrics`, `observability`, `notifications`,
`claustrum` — each documented below (`auth.reverse_proxy` is nested under
`auth`).

## `claude` — binary & bridge spawn (`ClaudeConfig`)

<!-- BEGIN GEN: claude -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `binary` | str | `claude` | The `claude` binary name or path (resolved to an absolute path before spawning). |
| `min_version` | str | `2.1.145` | Minimum acceptable `claude` version. |
| `agents_json_poll_interval_seconds` | int | `300` | How often (≥1) the inspector cross-checks `claude agents --json` for liveness. |
| `startup_grace_seconds` | float | `60.0` | How long (>0) a freshly-spawned bridge may stay alive without registering an environment before it is marked `ERROR`. Liveness alone is not "running". |
| `auto_enable_remote_control` | bool | `true` | Before the first spawn, mark remote control acknowledged (`hasUsedRemoteControl` / `remoteDialogSeen`) in `~/.claude.json` so a detached-stdin bridge isn't stuck on the one-time "Enable Remote Control? (y/n)" prompt. Set `false` to manage it yourself. |
| `resume_recap` | bool | `false` | Install a `SessionStart` hook in the runtime user's `~/.claude/settings.json` that recaps the most recent prior transcript for the cwd into a restarted (standard-mode) bridge. Opt-in: edits the user's Claude settings and injects prior turns. |
| `resume_recap_max_chars` | int | `8000` | Character budget (≥500) for the recap injection (most recent turns kept). |
| `resume_mode` | `standard` \| `pty` | `standard` | Launch mode for **new** bridges. `pty` = native true-resume under a PTY keeper (POSIX only; falls back to standard on Windows). A bridge keeps the mode it launched with — editing this never re-modes a running or stopped bridge. |
<!-- END GEN: claude -->

## `instance_defaults` — new-bridge defaults (`InstanceDefaults`)

<!-- BEGIN GEN: instance_defaults -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `spawn_mode` | `same-dir` \| `worktree` \| `session` | `same-dir` | Default spawn mode for new bridges. `worktree` requires a git repo. |
| `permission_mode` | `default` \| `plan` \| `acceptEdits` \| `auto` \| `dontAsk` \| `bypassPermissions` | `default` | Default permission mode for new bridges. |
| `session_name_prefix` | str \| null | `null` | Optional prefix for auto-generated Remote Control session names (maps to `claude remote-control --remote-control-session-name-prefix`); applies to the standard multi-session bridge only. Unset → claude's default (the hostname). |
| `capacity` | int | `32` | Max concurrent sessions a single standard bridge runs in `same-dir`/`worktree` spawn mode (≥1); passed to `claude remote-control --capacity`. Ignored for `session` spawn mode and the pty bridge (both single-session). |
| `max_bridges` | int \| null | `null` | Best-effort clauster cap on concurrent remote-control bridges (standard/pty; ≥1) — NOT hosted/bg-agent sessions. A bridge spawn over the cap is refused (409); cross-project concurrent spawns may transiently overshoot by a few. Unset → no limit. Distinct from `capacity` (per-bridge sessions). |
<!-- END GEN: instance_defaults -->

Permission modes: `default`, `plan`, `acceptEdits`, `auto`, `dontAsk`,
`bypassPermissions`. `bypassPermissions` is footgun-gated — see
`projects.<name>.allow_bypass_permissions` below.

## `projects` — per-project map (`ProjectConfig`)

A map of project name → settings. Additive-only; unknown keys ignored.

<!-- BEGIN GEN: projects -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `allow_bypass_permissions` | bool | `false` | The **hard ceiling** for the bypassPermissions footgun gate. A project can never be spawned with `--permission-mode bypassPermissions` unless this is set here in `clauster.yml`. The dashboard's per-session typed-confirm is the second layer. |
<!-- END GEN: projects -->

```yaml
projects:
  my-repo:
    allow_bypass_permissions: true
```

## `auth` — authentication (`AuthConfig`)

<!-- BEGIN GEN: auth -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | **Master auth switch.** Must be `true` for password / reverse-proxy auth to actually gate requests. |
| `password_required` | bool | `false` | Require password login. Needs `password_hash`. |
| `password_hash` | str \| null | `null` | argon2id hash from `clauster hash-password`. |
| `api_token_hash` | str \| null | `null` | SHA-256 hash of an inbound API bearer token from `clauster hash-token`. Enables `Authorization: Bearer <token>` auth for headless/API clients. Only the hash is stored; the raw token is shown once. |
| `allow_unauthenticated_network` | bool | `false` | Explicit opt-out: permit a non-loopback bind **without** enforced auth (e.g. a trusted LAN). `ops._check_auth` downgrades this to a warning. |
| `cookie_secure` | `auto` \| `always` \| `never` | `auto` | Session-cookie `Secure` flag. `auto` = Secure only over https (or a trusted proxy's `X-Forwarded-Proto=https`). |
| `session_max_age_seconds` | int | `604800` | Session lifetime (≥1; default 7 days). |
| `allowed_origins` | list[str] | `[]` | Extra WebSocket / CSRF origins (e.g. the proxy domain). |
<!-- END GEN: auth -->

### `auth.reverse_proxy` (`ReverseProxyConfig`)

<!-- BEGIN GEN: reverse_proxy -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Enable trusted-reverse-proxy auth. |
| `user_header` | str | `Remote-User` | Header carrying the authenticated user. |
| `shared_secret_header` | str | `X-Proxy-Auth` | Header carrying the HMAC signature. |
| `trusted_ips` | list[str] | `[]` | Peer-IP allowlist for the proxy. |
| `shared_secret` | str \| null | `null` | HMAC key the proxy signs `X-Proxy-Auth` with. |
| `hmac_window_seconds` | int | `60` | Clock-skew / replay window (≥0). |
<!-- END GEN: reverse_proxy -->

!!! danger "Two fail-closed validators"
    1. A **non-loopback** `host` is refused unless auth is *actually enforced* —
       `auth.enabled: true` **plus** either `password_required` or
       `reverse_proxy.enabled` — or you explicitly set
       `allow_unauthenticated_network`. Setting a password **without**
       `enabled: true` is a silent open door, so the validator rejects it.
    2. `password_required` with an empty `password_hash` is refused (it would
       lock everyone out or be silently skipped).

See [Security](security.md) and [Networking](networking.md) for the full matrix.

## `logs` — bridge-log rotation & redaction (`LogsConfig`)

<!-- BEGIN GEN: logs -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `bridge_log_max_size_mb` | int | `10` | Per-bridge debug-log rotation size (≥1 MB). |
| `keep_rotated` | int | `5` | Number of rotated log files to keep (≥0). |
| `redact_session_url` | bool | `false` | `false` = hybrid: the bridge debug log is verbatim on disk, redacted only over the WebSocket. `true` also redacts the on-disk bridge debug log — the bridge writes a private `0600` raw copy (which Clauster still parses for readiness + the deep link) and the public log becomes a redacted mirror of it. Scope is the bridge log only: the pty keeper sidecar and `state.json` still record session/environment ids as operational state, protected by `state_dir` permissions. |
| `strip_ansi_in_stream` | bool | `true` | Strip ANSI escape sequences from the streamed log. |
| `retention_max_age_days` | int | `30` | Delete a spawn's bridge-log set once its newest file is older than this many days (`0` = keep forever). Bounds unbounded disk growth and at-rest retention of session logs (which by default include the session URL). Pruned on each spawn. |
| `retention_max_files` | int | `0` | Keep at most this many of the most recent bridge-log sets, deleting the oldest beyond it (`0` = unlimited). A 'set' is one spawn's `.log` + its `.raw.log` / `.stderr.log` / `.keeper.json` siblings. |
| `retention_max_total_mb` | int | `0` | Cap the total size of the bridge-logs directory in MB, deleting the oldest sets until under the cap (`0` = unlimited). |
<!-- END GEN: logs -->

## `clone` — project clone/create guards (`CloneConfig`)

Clone URLs are user-supplied and hit the network from the host, so defaults are
strict.

<!-- BEGIN GEN: clone -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | Allow cloning/creating projects. |
| `allowed_schemes` | list[str] | `["https", "ssh"]` | Permitted clone URL schemes. |
| `allow_private_hosts` | bool | `false` | Block private/LAN IP targets by default (SSRF guard). |
| `allowed_private_cidrs` | list[str] | `[]` | Targeted LAN opt-in. Each entry is validated as a CIDR at load (a malformed entry fails fast rather than silently never matching). |
| `timeout_seconds` | int | `300` | Clone timeout (≥1). |
| `max_mb` | int | `2048` | Post-clone size cap (≥0; `0` = unlimited). |
<!-- END GEN: clone -->

## `reaper` — ghost-environment reaper (`ReaperConfig`)

<!-- BEGIN GEN: reaper -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `ui_enabled` | bool | `false` | Expose the ghost-environment reaper in the **dashboard**. The CLI (`clauster reap-environments`) is always available; this gates only the destructive browser surface. |
<!-- END GEN: reaper -->

## `usage` — per-project cost/token badge (`UsageConfig`)

<!-- BEGIN GEN: usage -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `mode` | `cost` \| `tokens` \| `off` | `cost` | What the badge shows. `cost` = approximate cost (USD price table × `fx_rate`, prefixed with `currency_symbol`); `tokens` = total token count only; `off` = hide the badge and skip the `/api/projects/{name}/usage` fetch. (`mode: off` may be written unquoted — YAML's boolean `off` is coerced back.) |
| `currency` | str | `USD` | Currency code shown in the tooltip (normalized to upper-case). |
| `currency_symbol` | str \| null | `null` | Symbol rendered in `cost` mode. Defaults to `$` when `currency` is `USD`, otherwise the currency code. |
| `fx_rate` | float | `1.0` | **Static, user-supplied** multiplier applied to the USD cost before display (>0; no live FX lookup). Leave `1.0` for USD; a non-USD `currency` left at `1.0` logs a warning (it would label a USD figure with a foreign symbol). |
| `token_total_includes_cache` | bool | `true` | Whether cache (creation + read) tokens count toward the displayed token total; they usually dominate, so set `false` for a leaner figure. The per-category breakdown is always in the tooltip. |
| `show_cost` | bool | `true` | **Deprecated** back-compat alias: `show_cost: false` forces `mode: off`. |
<!-- END GEN: usage -->

!!! info "Cost is approximate"
    Token counts are exact (read from transcript `usage`); the dollar figure is
    a ballpark from a hand-maintained USD price table (`usage.py`) that drifts as
    pricing changes — unpriced models count as 0. `fx_rate` is a static,
    user-supplied multiplier (no live FX), so a non-USD `currency` needs an
    `fx_rate` to be meaningful.

## `metrics` — live per-bridge resource metrics (`MetricsConfig`)

A point-in-time sample of the bridge's process tree, shown only while a bridge
runs.

<!-- BEGIN GEN: metrics -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | Show the metrics line. When `false`, hides it **and** skips both the `/api/projects/{name}/metrics` fetch and the server-side sample. |
| `normalize_cpu` | bool | `false` | Divide summed CPU% by the host core count (0–100% of the machine) instead of the raw across-cores figure (which can exceed 100%). |
| `show_disk` | bool | `true` | Toggle the disk read/write rate portion. |
| `sample_interval_seconds` | float | `0.15` | Two-snapshot sampling window (>0, ≤2.0). Longer is steadier but each fetch blocks a worker thread for that long. |
| `poll_seconds` | float | `4.0` | Dashboard metrics refresh cadence (≥1.0), decoupled from the status poll. |
<!-- END GEN: metrics -->

## `observability` — read-only metrics endpoint (`ObservabilityConfig`)

A Prometheus exposition of point-in-time gauges, off by default and behind the
auth guard. See [Networking](networking.md) for scraping behind auth.

<!-- BEGIN GEN: observability -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `prometheus_enabled` | bool | `false` | Gate a text-format `/metrics` endpoint (build info, bridge counts by status, project count, per-bridge cpu/rss, crash counter, hosted/claustrum gauges). Off by default; when off, `/metrics` returns 404. The endpoint stays **behind** the auth guard unless `metrics_token` is set. |
| `metrics_token` | str \| null | `null` | Optional bearer token that lets a scraper (e.g. Prometheus) reach `/metrics` without a browser session — presented as `Authorization: Bearer <token>`. When set, a valid token OR a normal session grants access; when unset, `/metrics` stays behind the auth guard. Compared in constant time. Supply via `CLAUSTER_OBSERVABILITY_METRICS_TOKEN_FILE` to keep it out of the config file. |
<!-- END GEN: observability -->

## `notifications` — outbound alerts via Apprise (`NotificationsConfig`)

Best-effort, fail-closed notifications on bridge lifecycle events. Off by default
and requires the optional `notify` extra:

```sh
pip install 'clauster[notify]'    # or: uv tool install 'clauster[notify]'
```

If `enabled` but the extra isn't installed, Clauster logs a warning at startup and
sends nothing — a notification failure never affects a bridge's lifecycle.

<!-- BEGIN GEN: notifications -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Master switch for outbound notifications. |
| `urls` | list[str] | `[]` | Apprise notification URLs (e.g. `slack://`, `discord://`, `tgram://`). Requires the `notify` extra. A non-loopback secret in a URL is the operator's responsibility to keep out of shared configs. |
| `notify_on_crash` | bool | `true` | Notify when a bridge exits unexpectedly (CRASHED — i.e. not via the Stop button). |
<!-- END GEN: notifications -->

```yaml
notifications:
  enabled: true
  urls:
    - "slack://tokenA/tokenB/tokenC/#alerts"
    - "tgram://bottoken/ChatID"
  notify_on_crash: true
```

See the [Apprise URL list](https://github.com/caronc/apprise/wiki) for supported
services.

## `claustrum` — hosted live-view channel (`ClaustrumConfig`)

**Experimental / in development.** Off by default. When `enabled`, Clauster
connect-or-spawns a single `claustrum` daemon per deployment (the maintainer's Go
`claude-ssh` reimplementation) at startup and surfaces its health under
`/healthz`. The daemon self-daemonizes, so it survives a Clauster restart and
Clauster simply reconnects. Fail-closed: an unreachable daemon or a rejected auth
token is reported in health and never affects the bridge lifecycle.

The socket and a `0600` auth token live under `<state_dir>/claustrum/` (`0700`).
Hosted sessions get their own dashboard panel — start a session, watch it stream
live, drive it, approve/deny tool prompts, and resume or recover it after a
restart (see [Architecture](architecture.md)).

<!-- BEGIN GEN: claustrum -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Master switch for the claustrum hosted channel. When true, Clauster connect-or-spawns the daemon at startup. |
| `binary` | str | `claustrum` | The `claustrum` binary name or path (resolved to an absolute path before spawning). |
| `socket_path` | str \| null | `null` | Path to the daemon's AF_UNIX socket. Defaults to `<state_dir>/claustrum/daemon.sock`. |
| `spawn_timeout_seconds` | float | `10.0` | How long (>0) to wait for a freshly spawned daemon to detach and accept its first connection before giving up. |
| `keep_children` | bool | `true` | Spawn the daemon with -keep-children so a daemon restart/upgrade leaves hosted sessions running (Clauster reattaches or offers recovery on reconnect). Set false for clean-slate-on-restart. POSIX-only (the daemon ignores it with a warning on Windows). |
| `request_timeout_seconds` | float | `30.0` | Per-request timeout (>0) for RPCs on the daemon connection. |
<!-- END GEN: claustrum -->

```yaml
claustrum:
  enabled: true
  binary: claustrum
```

## Minimal example

```yaml
# loopback, no auth needed
projects_root: ~/code
```

## LAN example (password auth)

```yaml
projects_root: ~/code
host: 0.0.0.0
port: 7621
auth:
  enabled: true
  password_required: true
  password_hash: "$argon2id$v=19$..."   # from `clauster hash-password`
  cookie_secure: always                 # if no TLS-terminating proxy
```
