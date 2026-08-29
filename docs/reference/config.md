# Configuration reference

The exhaustive field reference for `clauster.yml` — every section, every key.
The tables below are **generated from the pydantic models** in
`src/clauster/config.py` (via `scripts/gen_config_reference.py`), so they never
drift from the code — a CI check fails the build if a field is added or changed
without regenerating this page.

How a config file is found, environment-variable overrides, and `*_FILE` secret
files are covered in [Configuring Clauster](../guides/configuration.md).
Changing settings from the browser is covered in
[the in-app config editor](../guides/config-editor.md).

## Top level (`ClausterConfig`)

<!-- BEGIN GEN: clauster -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `schema_version` | int | `1` | Config schema version (additive-only). |
| `projects_root` | path | *(required)* | Directory whose children become project cards. Must exist, be a directory, and be readable — validated at load. `~` is expanded. |
| `host` | str | `127.0.0.1` | Bind address. A non-loopback host requires enforced auth (see Networking). |
| `port` | int | `7621` | Bind port (1–65535). |
| `state_dir` | path | `~/.clauster` | Where `clauster.db` and runtime state live (`state.json` is a legacy import source). `~` is expanded. |
| `root_path` | str | `""` | ASGI `root_path` for serving under a reverse-proxy sub-path. |
| `log_format` | `text` \| `json` | `text` | Application log format. `text` (default) is the human single-line format; `json` emits one structured JSON object per record. Both modes redact session URLs / bearer ids before the line is written. |
| `log_level` | `debug` \| `info` \| `warning` \| `error` | `info` | Minimum severity written to the application log, applied to Clauster's own loggers and uvicorn's alike. `debug` surfaces the server-side detail a bug report needs — auth rejections, spawn validation, reconcile passes, claustrum lifecycle — and is meant for diagnosis rather than steady state: it is high-volume, and while every line is still redacted the same way (see `log_format`), it records far more about what the server is doing, so the log is worth treating as sensitive. Names are case-insensitive (`DEBUG` works). Applied once at startup — restart Clauster for a change to take effect. |
| `instance_name` | str \| null | `null` | Optional label (≤32 chars, `[A-Za-z0-9_.-]`). When set, retitles the process to `clauster[<name>]` so co-resident instances are distinguishable in `ps`/`pgrep`. Cosmetic only. |
| `tls` | TlsConfig \| null | `null` | Native HTTPS termination. Unset (default) = serve plain HTTP and rely on a reverse proxy / `tailscale serve` for TLS. Set `tls` to have Clauster terminate TLS itself. Two modes: `provision = off` (default) requires `cert_file` + `key_file` pointing at an existing cert + key (validated fail-closed); `provision = self-signed` generates a self-signed cert+key under `state_dir/tls/` automatically (`cryptography` package required). ACME is deferred to issue 774. |
<!-- END GEN: clauster -->

Nested sections: `claude`, `instance_defaults`, `projects`, `auth`, `api`, `ui`,
`logs`, `clone`, `reaper`, `usage`, `metrics`, `observability`, `notifications`,
`webhooks`, `claustrum`, `tls` — each documented below (`auth.reverse_proxy` is
nested under `auth`).

## `claude` — binary & bridge spawn (`ClaudeConfig`)

<!-- BEGIN GEN: claude -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `binary` | str | `claude` | The `claude` binary name or path (resolved to an absolute path before spawning). |
| `min_version` | str | `2.1.145` | Minimum acceptable `claude` version. |
| `agents_json_poll_interval_seconds` | int | `30` | How often (≥1) the inspector cross-checks `claude agents --json` for liveness; lower = snappier live indicators + crash detection, at the cost of more subprocess spawns. |
| `startup_grace_seconds` | float | `60.0` | How long (>0) a freshly-spawned bridge may stay alive without registering an environment before it is marked `ERROR`. Liveness alone is not "running". |
| `auto_enable_remote_control` | bool | `true` | Before the first spawn, mark remote control acknowledged (`hasUsedRemoteControl` / `remoteDialogSeen`) in `~/.claude.json` so a detached-stdin bridge isn't stuck on the one-time "Enable Remote Control? (y/n)" prompt. Set `false` to manage it yourself. |
| `resume_recap` | bool | `false` | Install a `SessionStart` hook in the runtime user's `~/.claude/settings.json` that recaps the most recent prior transcript for the cwd into a restarted (standard-mode) bridge. Opt-in: edits the user's Claude settings and injects prior turns. |
| `resume_recap_max_chars` | int | `8000` | Character budget (≥500) for the recap injection (most recent turns kept). |
| `launch_mode` | `standard` \| `pty` | `standard` | Launch mode for **new** bridges. `pty` = native true-resume under a PTY keeper (a POSIX pty, or a ConPTY keeper on Windows with the `pty` extra installed; falls back to standard when that extra is absent). A bridge keeps the mode it launched with — editing this never re-modes a running or stopped bridge. (Renamed from `resume_mode`, still accepted as a deprecated alias.) |
| `pty_screen_enabled` | bool | `false` | (pty mode) Publish a redacted, read-only render of the bridge's live terminal screen for the dashboard's live-terminal view (#534). Off by default; needs the optional `pyte` dependency (`pip install 'clauster[pty]'`) — without it the feature stays dormant. The standalone binary does not bundle `pyte` (LGPL): install it there with `clauster deps install pty` (#904), or manually side-load `pyte` by setting `CLAUSTER_PYTE_PATH` to a directory holding an installed `pyte` (#702) — the binary appends it to `sys.path` only when set. The render is best-effort secret-redacted, so treat the live view as auth-gated, not secret-proof. |
| `path_append` | list[str] | `[]` | Directories appended to the bridge subprocess `PATH` so a `claude` session can resolve user-local tools (e.g. `~/.local/bin`) that a minimal service `PATH` omits. `~` is expanded; entries are appended in order after the inherited `PATH`, never replacing it. Applies to both standard and pty bridges. |
| `node_from_nvm` | bool | `true` | Resolve nvm's `default` node version at each bridge spawn and prepend its bin dir to the bridge subprocess `PATH` (before the base PATH). Puts `node`/`npx`/`npm` AND any nvm-global CLI (e.g. `agent-browser`) — all of which live in that one bin dir — on the raw process `PATH`, so they resolve in EVERY spawn context, not just `bash -c`: dash/`sh -c`, direct `execvp` (how Claude Code spawns MCP stdio servers), and subagents all inherit it, unlike a `BASH_ENV` nvm-init which only non-interactive bash sources. Fixes `npx`/`node`-based MCP servers (e.g. codecov, context7) showing `✘ Failed to connect` under a systemd deployment. On by default and fail-safe: a no-op (never raises) when nvm, its `default` alias, or POSIX `bash` aren't available — spawn is never blocked by this. Prepended (not appended, #1018) so nvm's node WINS over a distro `node` already on the base `PATH` (e.g. `/usr/bin/node`) — so it also takes precedence over a `node` in `path_append`. POSIX-only (nvm is a bash function); ignored on Windows. Set to `false` to opt out (e.g. you pin node another way). |
<!-- END GEN: claude -->

The `claude.env` map (filtered from the generated table because it's a dict) overlays
extra environment variables onto the bridge subprocess (both standard and pty modes).
It is applied **after** Clauster's secret scrub, so a key matching a Clauster secret name
(`CLAUSTER_*` carrying `SECRET`/`PASSWORD`/`TOKEN`/`HASH`) is dropped and can never
re-introduce a scrubbed credential. Pair it with `claude.path_append` to extend the
bridge subprocess `PATH`:

```yaml
claude:
  path_append:
    - "~/.cargo/bin"
    - "~/go/bin"
  env:
    FOO: "bar"
```

A unit generated by [`clauster install-service`](../installation.md#run-as-a-systemd-service-linux)
already bakes `~/.local/bin` + the system dirs into the service `PATH`, so `path_append`
is for what a static directory can't cover — **shell-managed** toolchains like nvm/pyenv
`node`, `cargo`, or Go — and for deployments that don't use the generated unit (Docker,
manual launch). Entries append to (never replace) the inherited `PATH`.

### npx/node MCP servers under systemd (nvm)

An `npx`- or `node`-based MCP server in your `~/.claude.json` (e.g. `codecov`,
`context7`, and most published servers) can show `✘ Failed to connect` in a bridge
— visible via `claude mcp list` run inside one — while connecting fine in an
interactive `claude` TUI. This bites the **systemd deployment mode** specifically:
Claude Code launches MCP **stdio** servers by spawning their configured `command`
**directly** (`execvp` / `sh -c`; `sh` is dash and ignores `BASH_ENV`), not through
a login or non-interactive bash shell, so only the bridge subprocess `PATH` itself
matters — and nvm's bin dir is version-specific, so it's never baked into a static
`path_append`. Interactive-launch deployments inherit `node` from your shell and
never hit this.

`claude.node_from_nvm` (**on by default**) fixes it: at each bridge spawn,
Clauster resolves nvm's `default` node version (`nvm which default`, same
resolution nvm itself uses) and **prepends** its bin dir to the bridge `PATH`, before
the base `PATH` — so nvm's node wins over a distro `node` already on `PATH`, and takes
precedence over a `node` in `path_append` too (#1018). It tracks nvm's current default across node upgrades — no shim
script to maintain — and is a no-op (never blocks a spawn) when nvm or a `default`
alias isn't found. POSIX-only (nvm is a bash function); see the `node_from_nvm`
row in the table above.

## `instance_defaults` — new-bridge defaults (`InstanceDefaults`)

<!-- BEGIN GEN: instance_defaults -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `spawn_mode` | `same-dir` \| `worktree` \| `session` | `same-dir` | Default spawn mode for new **bridge** sessions (the standard / pty launch flow) — where the session's working directory lives. `worktree` requires a git repo; `session` runs in a fresh sandbox. Bridge launches only — hosted (browser) sessions ignore this. |
| `permission_mode` | `default` \| `plan` \| `acceptEdits` \| `auto` \| `dontAsk` \| `bypassPermissions` | `default` | Default permission mode for new bridges. |
| `verbose` | bool | `false` | Pass `--verbose` to spawned standard `claude remote-control` bridges for detailed connection/session logs — every spawn mode (same-dir/worktree/session). The pty (flag-form) bridge is never passed --verbose. Off by default. |
| `session_name_prefix` | str \| null | `null` | Optional prefix for auto-generated Remote Control session names (maps to `claude remote-control --remote-control-session-name-prefix`); applies to the standard multi-session bridge only. Unset → claude's default (the hostname). |
| `capacity` | int | `32` | Max concurrent sessions a single standard bridge runs in `same-dir`/`worktree` spawn mode (≥1); passed to `claude remote-control --capacity`. Ignored for `session` spawn mode and the pty bridge (both single-session). |
| `max_bridges` | int \| null | `null` | Best-effort clauster cap on concurrent remote-control bridges (standard/pty; ≥1) — NOT hosted/bg-agent sessions. EVERY live bridge counts, including further bridges of the project being started: one project can hold a standard bridge and several interactive sessions at once, and all of them count. A bridge spawn over the cap is refused (409); cross-project concurrent spawns may transiently overshoot by a few. Unset → no limit. Distinct from `capacity` (per-bridge sessions). |
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
| `enabled` | bool | `false` | **Master auth switch.** Must be `true` for password / reverse-proxy auth to actually gate requests. The `false` default is safe only on loopback: a non-loopback bind **refuses to start** without enforced auth (fail-closed) unless `allow_unauthenticated_network` explicitly opts out. |
| `password_required` | bool | `false` | Require password login. Needs `password_hash`. |
| `password_hash` | str \| null | `null` | argon2id hash from `clauster hash-password`. |
| `api_token_hash` | str \| null | `null` | SHA-256 hash of an inbound API bearer token from `clauster hash-token`. Enables `Authorization: Bearer <token>` auth for headless/API clients. Only the hash is stored; the raw token is shown once. |
| `allow_unauthenticated_network` | bool | `false` | Explicit opt-out: permit a non-loopback bind **without** enforced auth (e.g. a trusted LAN). `ops._check_auth` downgrades this to a warning. When `auth.enabled` is `false`, **anyone who can reach the port has full operator control of this host** — the dashboard drives a shell; treat it accordingly. A non-loopback bind auto-allows no `Origin`, so pair this with `allowed_origins` (the cross-site gate runs even with auth off) or the dashboard's own writes and live views are rejected. |
| `cookie_secure` | `auto` \| `always` \| `never` | `auto` | Session-cookie `Secure` flag. `auto` = Secure only over https (or a trusted proxy's `X-Forwarded-Proto=https`). |
| `session_max_age_seconds` | int | `604800` | Session lifetime (≥1; default 7 days). |
| `allowed_origins` | list[str] | `[]` | Extra WebSocket / CSRF origins (e.g. the proxy domain). The `Origin` allowlist is enforced on unsafe methods and WebSocket handshakes **even when `enabled` is `false`** — it is a cross-site defence, not an authentication method. A loopback bind auto-allows only `127.0.0.1`/`localhost`/`[::1]` **at the configured port**, so list your real browser-facing origin here whenever it differs: a non-loopback bind, a reverse proxy or tunnel, or an SSH port-forward onto a different local port. |
<!-- END GEN: auth -->

### `auth.reverse_proxy` (`ReverseProxyConfig`)

<!-- BEGIN GEN: reverse_proxy -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Enable trusted-reverse-proxy auth. |
| `user_header` | str | `Remote-User` | Header carrying the authenticated user. |
| `shared_secret_header` | str | `X-Proxy-Auth` | Header carrying the HMAC signature. |
| `trusted_ips` | list[str] | `[]` | Peer-IP allowlist for the proxy. Each entry is an IP or CIDR, validated at load (a malformed entry fails fast rather than silently never matching). |
| `shared_secret` | str \| null | `null` | HMAC key the proxy signs `X-Proxy-Auth` with. |
| `hmac_window_seconds` | int | `60` | Clock-skew / replay window (≥0). |
| `require_hmac` | bool | `true` | When true (default, higher assurance), a request from a `trusted_ips` peer must also carry a valid HMAC in `shared_secret_header` to authenticate. Set false ONLY behind a forward-auth proxy (Authelia, authentik, Caddy `forward_auth`, Traefik, oauth2-proxy) that asserts `user_header` but signs no HMAC: clauster then trusts `user_header` from a trusted peer alone — so the proxy MUST strip that header from inbound client requests and be the sole route to clauster, since anyone able to reach a `trusted_ips` peer can forge the user. |
<!-- END GEN: reverse_proxy -->

!!! danger "Two fail-closed validators"
    1. A **non-loopback** `host` is refused unless auth is *actually enforced* —
       `auth.enabled: true` **plus** either `password_required` or
       `reverse_proxy.enabled` — or you explicitly set
       `allow_unauthenticated_network`. Setting a password **without**
       `enabled: true` is a silent open door, so the validator rejects it.
    2. `password_required` with an empty `password_hash` is refused (it would
       lock everyone out or be silently skipped).

See [Security](../security.md) and [Networking](../networking.md) for the full matrix.

## `api` — the versioned `/api/v1` public surface (`ApiConfig`)

<!-- BEGIN GEN: api -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `openapi_enabled` | bool | `false` | Serve the OpenAPI docs (`/docs`) and schema (`/openapi.json`). **Off by default** — the documented HTTP surface isn't exposed until explicitly opted in. When `true`, both paths still require the same authentication as any `/api/...` route (a session cookie, reverse-proxy auth, or a Bearer token) whenever `auth.enabled` is set; an unauthenticated request gets a `401`, not a login redirect. |
<!-- END GEN: api -->

The dashboard's ~60 unversioned `/api/...` routes are unchanged and keep serving
the Alpine frontend. Alongside them, `/api/v1/...` aliases the public, stable
resource subset (project list, session reads, instance spawn/stop/resume, agent
spawn/stop/resume) under the same handlers and the same auth gate — see
[the public API guide](../public-api.md) for the full route list and the
`clauster api-token` CLI.

## `ui` — web-dashboard kill switch (`UiConfig`)

<!-- BEGIN GEN: ui -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | Serve the web dashboard (`/`, `/login`, `/logout`, `/static/*`) and the internal HTML-fragment / per-session interactive routes (`/api/projects/{name}/row`, `/api/widget`, the per-instance `message`/`permissions`/`forget`/`qr` routes). Default `true` — zero behavior change. Set `false` to serve only the JSON API (the rest of `/api/...`, `/api/v1/...`, `/healthz`, `/metrics`, and the WebSocket streams): the dashboard surface then 404s. Independent of `api.openapi_enabled` and `auth.*` — turning the API on never implies turning the UI off, or vice versa. Restart-required. |
<!-- END GEN: ui -->

Default `true` — zero behavior change. Set `ui.enabled: false` to serve **only**
the JSON API: the dashboard page, `/login` + `/logout`, `/static/*`, and the
internal HTML-fragment / per-session interactive routes (`/api/projects/{name}/row`,
`/api/widget`, and the per-instance `message`/`permissions/{request_id}`/`forget`/
`qr` routes — the same "internal, unversioned" set `api` above already excludes
from `/api/v1`) all 404. Every other route — the rest of the bare `/api/...` JSON
surface, `/api/v1/...`, `/healthz`, `/metrics`, and the WebSocket streams — keeps
working, still behind whatever auth is configured.

`ui.enabled` is **independent** of `api.openapi_enabled` and every `auth.*`
setting: enabling the API never implies disabling the UI, or vice versa — you
can run web-UI+API, API-only, UI-only (docs off), or any other combination.
Restart-required, and **not** web-editable (a browser toggle that can kill the
browser surface is a footgun with no way back in from the browser — see the
[in-app config editor](../guides/config-editor.md)).

!!! warning "No login page means no session-cookie auth"
    With the UI off there is no `/login` route, so session-cookie (and
    password) auth is unreachable. Only a Bearer token (`auth.api_token_hash`
    or a named `clauster api-token`) or a trusted reverse proxy can still
    authenticate. If `auth.enabled` is on and neither is configured, Clauster
    logs a loud startup warning — it does **not** refuse to start, since that
    would brick a deployment that flips `ui.enabled` off before minting a
    token. See [API-only deployment](../public-api.md#api-only-deployment) in the
    public API guide.

## `db` — persistence-layer knobs (`DbConfig`)

<!-- BEGIN GEN: db -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `backup_before_migrate` | bool | `true` | Snapshot `clauster.db` (via `VACUUM INTO`) to `state_dir/backups/` before running a **pending** Alembic migration — never on a plain restart already at head. The last 5 pre-migration snapshots are kept, older ones pruned. A snapshot write failure is logged as a WARNING and startup proceeds (the migration itself is transactional and safe on its own); set this to `false` to skip the snapshot attempt entirely. |
<!-- END GEN: db -->

**Recovery.** Each pre-migration snapshot is a self-contained copy of
`clauster.db` named `pre-<revision-before>-<revision-after>-<timestamp>.db`
under `state_dir/backups/`, written just before a migration that changed the
schema. To roll back: stop Clauster, replace `state_dir/clauster.db` with the
desired `state_dir/backups/pre-*.db` snapshot, **delete any stale
`clauster.db-wal` / `clauster.db-shm` sidecars** (the snapshot is a
self-contained `VACUUM INTO` copy; a leftover WAL belongs to the migrated
database and must not be replayed), then start the matching (older) `clauster`
binary — a newer binary
would immediately re-run the same migration against the restored file. This is
complementary to `clauster backup` / `restore` (a full `state_dir` + config
tarball); the snapshot here is automatic, DB-only, and scoped to the migration
path.

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
| `allow_private_hosts` | bool | `false` | When false (default), block clone URLs whose host is a private/LAN/loopback IP (SSRF guard); when true, allow them — prefer `allowed_private_cidrs` for a targeted opt-in over opening every private range. |
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

## `config_write` — code-executing config-write trust tier (`ConfigWriteConfig`)

A fail-closed trust tier (#347/#687) for writing Claude Code's *own* configuration
(MCP servers, hooks, permission rules, skills) from the dashboard. Each of those is
code the spawned `claude` executes as the clauster runtime user, so the gate assumes
the browser is the threat: both flags default **off**, and — unlike the operational
fields above — they are **never** web-editable. The capability is file/CLI-managed
only (exactly like the auth/bind/secret fields), so a browser session can never grant
itself the write capability. When `enabled` is off, the entire surface returns `404`.
User-scope writes (`~/.claude.json`) require the separate `allow_user_scope` opt-in on
top of `enabled`.

<!-- BEGIN GEN: config_write -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Master switch for the code-executing config-write capability (MCP servers / hooks / permission rules / skills). Off by default; the whole dashboard surface 404s when off. **Not** web-editable — file/CLI-managed only, exactly like the auth/bind/secret fields, because it is an RCE surface. |
| `allow_user_scope` | bool | `false` | A **second, independent** opt-in for user-scope writes (`~/.claude.json` / `~` settings), which affect every project and the live account — strictly more dangerous than a single project's `.mcp.json`. Project-scope can run with this off. Off by default; **not** web-editable. |
<!-- END GEN: config_write -->

## `login_shepherd` — dashboard-driven `claude` account login (`LoginShepherdConfig`)

A fail-closed gate (#839) for a dashboard panel that drives a **live OAuth login**
for the runtime `claude` account (`claude auth login` / `claude setup-token`),
so an operator whose runtime account has logged out (or whose token expired) can
fix it from the browser instead of SSHing in. This writes the runtime user's own
Claude Code credentials, so — mirroring `config_write.enabled` — it defaults
**off** and is **never** web-editable; when `enabled` is off, the entire
`/api/login-shepherd/*` surface returns `404` and the dashboard panel doesn't render.

The `setup-token` mode is gated behind a **second, independent opt-in**,
`allow_setup_token` (#846) — mirroring `config_write.allow_user_scope` — because it
mints a long-lived `CLAUDE_CODE_OAUTH_TOKEN` the operator copies out of the browser,
a durable credential strictly more dangerous than the ordinary `login` mode's
short-lived OAuth handshake. With `enabled` on but `allow_setup_token` off, only the
`login` (subscription sign-in) mode is offered — the dashboard panel shows a single
sign-in mode with no mode picker, and a `setup-token` start request `404`s exactly
like the whole surface being disabled, never a distinct `403` that would reveal the
mode exists.

`claude setup-token` is a full TUI that only renders its authorize link under a
real terminal (#846), so that mode is driven over a pty and needs the same
optional `pyte` dependency as the live pty-screen view — `pip install
'clauster[pty]'` on a package install, `clauster deps install pty` on the
standalone binary (or `CLAUSTER_PYTE_PATH`; see `pty_screen_enabled` above). Without it, picking "Create a long-lived token"
fails closed with a message pointing at subscription sign-in (`claude auth
login`) instead, which stays on its original plain-pipe transport and needs no
extra dependency.

<!-- BEGIN GEN: login_shepherd -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Master switch for the dashboard login-shepherd surface (`claude auth login` / `claude setup-token`, driven from the browser). Off by default; the whole surface 404s when off, same invisible-surface invariant as `config_write.enabled` and the reaper UI. |
| `allow_setup_token` | bool | `false` | A **second, independent** opt-in for the `setup-token` mode (`claude setup-token`), which mints a long-lived `CLAUDE_CODE_OAUTH_TOKEN` the operator copies out of the browser — a durable credential, strictly more dangerous than the ordinary `login` mode's short-lived OAuth handshake. Requires `login_shepherd.enabled` too; with this off, only the `login` mode is offered (a `setup-token` request 404s, the same invisible-surface shape as the whole disabled surface). Off by default; **not** web-editable. |
<!-- END GEN: login_shepherd -->

### Non-interactive runtime-account auth (apiKeyHelper / API key / token)

Some deployments authenticate the runtime `claude` account **without** an interactive
login. These historically required SSH; the config editor lets you set them from the
dashboard, and the login-shepherd panel links to them next to the interactive flows.

Runtime-account auth is **account-wide**, so it belongs in **User**-scope `settings.json`
(`~/.claude/settings.json`) — a project's `settings.json` would only affect that project
and would leave the account itself unsigned-in. Editing User scope requires **both**
`config_write.enabled` and `config_write.allow_user_scope`; the login panel links
straight to the User-scope settings editor when both are on, and shows a hint to enable
them (or use SSH) when they are not.

| Mechanism | Where it goes (User scope) | When to use it |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | an **`env`** row (config editor → User → Settings → Rows) | Console / API (pay-as-you-go) billing rather than a Claude subscription. |
| `CLAUDE_CODE_OAUTH_TOKEN` | an **`env`** row | A long-lived token minted by `claude setup-token` (the same value the shepherd's `setup-token` mode prints) — pin it once instead of re-running the OAuth handshake. |
| `apiKeyHelper` | a top-level `settings.json` key (config editor → User → Settings → **Raw JSON**) | A custom command that mints/rotates the auth value on demand. Set `CLAUDE_CODE_API_KEY_HELPER_TTL_MS` (an `env` row) to control how often it is re-invoked. |

Secret-shaped values are masked on read and written through the config editor's
redaction path — the same surface as any other `env` row or `settings.json` key. The
login detector (`claude auth status --json`) already reports all three as logged-in, so
an account configured this way is **not** nagged to sign in.

## `mcp` — `clauster mcp` write-tool gate (`McpConfig`)

The [`clauster mcp`](../mcp.md) stdio server exposes read tools (`list_sessions` /
`session_status`) always, and **write** tools (`spawn_session` / `stop_session` /
`resume_session`) only when `allow_writes` is on. The stdio transport is
local-privileged and **unauthenticated** by design, so the write surface is gated
behind this switch and defaults **off** — attaching the server to an agent cannot
start, stop, or resume a bridge until an operator opts in. Like the `config_write`
and `login_shepherd` gates it is **not** web-editable (file/CLI-managed only).

> **Changed in 1.0 (breaking):** the write tools shipped always-on in #950; they now
> default off. An MCP client that drove `spawn`/`stop`/`resume_session` needs
> `mcp.allow_writes: true`. See [UPGRADING](../upgrading.md).

<!-- BEGIN GEN: mcp -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `allow_writes` | bool | `false` | Expose the `clauster mcp` **write** tools (`spawn_session` / `stop_session` / `resume_session`) that start, stop, and resume bridges. Off by default: the stdio MCP surface is read-only (`list_sessions` / `session_status` only) until you opt in. The surface is local-privileged and unauthenticated, so turning this on lets any agent the server is attached to drive the bridge lifecycle. **Not** web-editable — file/CLI-managed only, like the auth / config_write / login_shepherd gates. |
<!-- END GEN: mcp -->

## `usage` — per-project cost/token badge (`UsageConfig`)

<!-- BEGIN GEN: usage -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `mode` | `cost` \| `tokens` \| `off` | `cost` | What the badge shows. `cost` = approximate cost (USD price table × `fx_rate`, prefixed with `currency_symbol`); `tokens` = total token count only; `off` = hide the badge and skip the `/api/projects/{name}/usage` fetch. (`mode: off` may be written unquoted — YAML's boolean `off` is coerced back.) |
| `currency` | str | `USD` | Currency code shown in the tooltip (normalized to upper-case). |
| `currency_symbol` | str \| null | `null` | Symbol rendered in `cost` mode. Defaults to `$` when `currency` is `USD`, otherwise the currency code. |
| `fx_rate` | float | `1.0` | **Static, user-supplied** multiplier applied to the USD cost before display (>0; no live FX lookup). Leave `1.0` for USD; a non-USD `currency` left at `1.0` logs a warning (it would label a USD figure with a foreign symbol). |
| `token_total_includes_cache` | bool | `true` | Whether cache (creation + read) tokens count toward the displayed token total; they usually dominate, so set `false` for a leaner figure. The per-category breakdown is always in the tooltip. |
| `show_cost` | bool | `true` | **Deprecated** back-compat alias. `usage.mode` is authoritative; `show_cost: false` maps to `mode: off` only when `mode` is unset (mode wins if both are set). |
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
| `enabled` | bool | `true` | Show the per-session resource metrics line — live CPU, memory, and disk I/O for each running bridge, each row reporting its own bridge. When `false`, the line is hidden and Clauster skips the work behind it entirely: no `/api/metrics` polling from the browser and no server-side resource sampling. |
| `normalize_cpu` | bool | `false` | Divide summed CPU% by the host core count (0–100% of the machine) instead of the raw across-cores figure (which can exceed 100%). |
| `show_disk` | bool | `true` | Toggle the disk read/write rate portion. |
| `sample_interval_seconds` | float | `0.15` | Two-snapshot sampling window (>0, ≤2.0). Longer is steadier but each fetch blocks a worker thread for that long. |
| `poll_seconds` | float | `4.0` | Dashboard metrics refresh cadence (≥1.0), decoupled from the status poll. |
<!-- END GEN: metrics -->

## `observability` — read-only metrics endpoint (`ObservabilityConfig`)

A Prometheus exposition of point-in-time gauges, off by default and behind the
auth guard. See [Networking](../networking.md) for scraping behind auth.

<!-- BEGIN GEN: observability -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `prometheus_enabled` | bool | `false` | Gate a text-format `/metrics` endpoint (build info, bridge counts by status, project count, per-project cpu/rss summed across that project's live bridges, crash counter, hosted/claustrum gauges). Off by default; when off, `/metrics` returns 404. The endpoint stays **behind** the auth guard unless `metrics_token_hash` is set. |
| `metrics_token_hash` | str \| null | `null` | SHA-256 hash of an optional bearer token that lets a scraper (e.g. Prometheus) reach `/metrics` without a browser session — the scraper presents the raw token as `Authorization: Bearer <token>`. When set, a valid token OR a normal session grants access; when unset, `/metrics` stays behind the auth guard. Only the hash is stored (parity with `auth.api_token_hash`); the raw token is shown once by `clauster hash-metrics-token`. Supply via `CLAUSTER_OBSERVABILITY_METRICS_TOKEN_HASH_FILE` to keep it out of the config file. |
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
| `enabled` | bool | `false` | Master switch for the outbound (Apprise) channel. |
| `urls` | list[str] | `[]` | Apprise notification URLs (e.g. `slack://`, `discord://`, `tgram://`). Requires the `notify` extra. A non-loopback secret in a URL is the operator's responsibility to keep out of shared configs. |
| `browser_enabled` | bool | `false` | Master switch for the browser (Web Notifications) channel — the dashboard shows a desktop notification once the browser grants permission. |
| `notify_on_crash` | bool | `true` | Notify when a bridge exits unexpectedly (CRASHED — i.e. not via the Stop button). |
| `notify_on_ready` | bool | `false` | Notify when a bridge finishes starting and becomes ready (RUNNING). |
| `notify_on_stop` | bool | `false` | Notify when a bridge is stopped normally (via the Stop button). |
| `notify_on_permission` | bool | `false` | Notify when a hosted session parks a tool-permission prompt — the 'come look' signal. |
| `notify_on_session_end` | bool | `false` | Notify when a session ends (a single-shot session bridge exits after its session completes). |
| `notify_on_reconnect_failed` | bool | `false` | Notify when a resume/reconnect attempt fails to bring a bridge back up. |
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

## `webhooks` — outbound lifecycle webhooks (`WebhooksConfig`)

Fail-open HTTP webhooks: each configured URL receives a JSON `POST` on a bridge
lifecycle transition (`spawn` / `ready` / `stop` / `crash`). Off by default. A slow
or failing endpoint is bounded by `timeout_seconds` and its error is logged and
swallowed — a webhook never blocks or breaks a spawn/stop. Only `http`/`https`
URLs are accepted (others are rejected at startup); URLs come only from this config.
Events fire for bridges Clauster spawns: a bridge **adopted** from an external
session or **reattached** on restart emits no `spawn`/`ready`.

<!-- BEGIN GEN: webhooks -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Master switch for outbound webhooks. |
| `urls` | list[str] | `[]` | HTTP(S) endpoint URLs that receive a JSON POST per lifecycle event. Only `http`/`https` schemes are accepted; others are rejected at startup. A secret embedded in a URL is the operator's responsibility to keep out of shared configs. |
| `timeout_seconds` | float | `10.0` | Per-request POST timeout in seconds (>0). A slow endpoint can't stall a lifecycle transition beyond this. |
| `block_private_targets` | bool | `false` | Opt-in SSRF guard. When True, skip any webhook URL whose host is — or resolves to — an internal/non-routable IP: loopback, link-local (incl. the 169.254.169.254 metadata IP), RFC1918 private, unspecified (0.0.0.0/::), reserved, multicast, IPv6 ULA (fc00::/7), and CGNAT (100.64/10) — including the non-canonical IPv4 encodings the resolver still honors (decimal-int, hex, short 127.1). A DNS hostname is resolved best-effort at filter time so a name pointing straight at a private IP can't bypass the guard; a rebinding domain that re-resolves at dial time is an acknowledged TOCTOU residual (same class as the clone-URL guard). Exotic IPv6 embeddings (NAT64, IPv4-compatible) are not normalized. Default False preserves the LAN-receiver use case. |
<!-- END GEN: webhooks -->

```yaml
webhooks:
  enabled: true
  urls:
    - "https://example.com/hooks/clauster"
  timeout_seconds: 10.0
  events:
    # Bridge events — absent key defaults to enabled.
    spawn: true
    ready: true
    stop: true
    crash: true
    # Extended events — absent key defaults to DISABLED; opt in explicitly.
    bg-settled: true
    permission-needed: true
    clone-done: true
```

The `events` map (filtered from the generated table because it's a dict) selects
which transitions emit. The four **bridge** keys — `spawn`, `ready`, `stop`,
`crash` — default to **enabled** when absent (so `events: {}` emits all four, and
you disable one with e.g. `crash: false`). The three **extended** keys —
`bg-settled` (a `claude --bg` job settled), `permission-needed` (a hosted session
parked a tool-permission prompt), `clone-done` (a clone finished) — default to
**disabled** when absent: you opt in to each, so a sensitive "come look" signal
never starts egressing on upgrade alone. An unsupported key is rejected at startup
rather than silently ignored.

Each bridge-event POST body is `{"event": "<name>", "project": ..., "label": ...,
"status": ..., "resume_mode": ..., "spawn_mode": ..., "session_ref": ...}`.
(The payload field `resume_mode` is the per-session bridge mode — `standard`/`pty`
— distinct from the renamed `claude.launch_mode` config key above; the wire field
keeps the older name.)
`session_ref` is a stable, non-reversible token (16 hex chars) derived from the
bridge's starter session id via HMAC-SHA256 keyed by the deployment's
session-signing secret — it lets a receiver correlate the lifecycle events of one
session without egressing the raw `session_<ULID>`, which is bearer-equivalent and
redacted elsewhere. Keying it with the secret means a receiver can't even verify a
guessed session id against the token. It is `null` until the bridge reports a
starter session.

The extended events carry an `event_type` discriminator instead and do **not**
reuse the bridge shape — see [Public API → Lifecycle webhooks](../public-api.md#lifecycle-webhooks)
for each body. Sensitive fields are redacted and credential-bearing values (the
clone URL, the raw session id, the permission-prompt body) are never sent.

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
restart (see [How it works](../concepts/how-it-works.md)).

**Getting the daemon.** The channel needs the `claustrum` binary. The supported way is
`clauster deps install claustrum` — it downloads the pinned, SHA-256-verified release for your
OS/arch from [`schubydoo/claustrum`](https://github.com/schubydoo/claustrum) into
`<state_dir>/deps/bin` and Clauster uses it automatically (no need to also set `binary`). An
explicit `binary` path, or a `claustrum` already on `PATH`, still wins if set. Like every managed
side-install it is fail-closed (a checksum mismatch refuses) and is **not** covered by Clauster's
own release signature — you are trusting that project's pinned release. `clauster deps list` shows
whether it is installed; `clauster deps uninstall claustrum` removes it.

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

## `tls` — native HTTPS termination (`TlsConfig`)

Unset by default — Clauster serves plain HTTP and you terminate TLS upstream (a
reverse proxy or `tailscale serve`). Set both `tls.cert_file` and `tls.key_file`
to have Clauster terminate TLS **itself** (uvicorn's `ssl_certfile`/`ssl_keyfile`),
which gives a secure context — required for browser features like Web
Notifications on a LAN IP — without a proxy.

<!-- BEGIN GEN: tls -->
| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `provision` | `off` \| `self-signed` | `off` | TLS provisioning mode. `off` (default) = supply `cert_file` + `key_file` pointing at an already-provisioned cert + key. `self-signed` = Clauster generates a self-signed cert+key under `state_dir/tls/` at startup (requires the `cryptography` package; regenerates on expiry or SAN change). ACME / Let's Encrypt is deferred to issue 774. |
| `hostnames` | list[str] | `[]` | Hostnames and/or IP addresses to include as Subject Alternative Names in the generated certificate. The first entry becomes the Common Name. Required when `provision = self-signed`; ignored otherwise. |
| `cert_file` | str \| null | `null` | Path to the PEM certificate (chain) file. `~` is expanded and the path is resolved to an absolute file at load — it must exist and be readable. Required when `provision = off`; must be absent when `provision = self-signed` (the cert is written by the provisioner). |
| `key_file` | str \| null | `null` | Path to the PEM private-key file. `~` is expanded and the path is resolved to an absolute file at load — it must exist and be readable. Required when `provision = off`; must be absent when `provision = self-signed` (the key is written by the provisioner). |
<!-- END GEN: tls -->

```yaml
host: 0.0.0.0
tls:
  cert_file: /etc/clauster/tls/fullchain.pem
  key_file: /etc/clauster/tls/privkey.pem
```

!!! danger "Fail-closed cert handling"
    Both paths are validated **twice** — at config-load and again at server start —
    for existence, readability, and that they resolve to an absolute file (any `..`
    is collapsed). If TLS is configured but a file is missing or unreadable, Clauster
    **aborts startup with a clear error** rather than silently serving plain HTTP.
    The key material is never logged. These fields are file/CLI-managed only and are
    deliberately **not** editable from the in-app config editor.

When TLS is active the connection is `https`, so `auth.cookie_secure: auto` marks
the session cookie `Secure` and the plain-http cookie warning is suppressed.
Self-signed-cert generation and ACME/Let's Encrypt are **out of scope** for this
block — point it at an already-provisioned cert + key. See
[Networking → Native HTTPS](../networking.md#native-https-built-in-tls).
