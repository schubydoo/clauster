# Configuration

All settings live in `clauster.yml`. The full, commented schema is in
[`clauster.yml.example`](https://github.com/schubydoo/clauster/blob/main/clauster.yml.example).
This page is the exhaustive field reference, derived from the pydantic models in
`src/clauster/config.py`.

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

**Schema is additive-only.** Old configs always validate against newer versions;
unknown per-project keys are ignored.

## Top level (`ClausterConfig`)

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `schema_version` | int | `1` | Config schema version (additive-only). |
| `projects_root` | path | *(required)* | Directory whose children become project cards. Must exist, be a directory, and be readable — validated at load. `~` is expanded. |
| `host` | str | `127.0.0.1` | Bind address. A non-loopback host requires enforced auth (see [Networking](networking.md)). |
| `port` | int | `7621` | Bind port (1–65535). |
| `state_dir` | path | `~/.clauster` | Where `state.json` and runtime state live. `~` is expanded. |
| `root_path` | str | `""` | ASGI `root_path` for serving under a reverse-proxy sub-path. |
| `log_format` | `text` \| `json` | `text` | Application log format. |
| `instance_name` | str \| null | `null` | Optional label (≤32 chars, `[A-Za-z0-9_.-]`). When set, retitles the process to `clauster[<name>]` so co-resident instances are distinguishable in `ps`/`pgrep`. Cosmetic only. |

Nested sections: `claude`, `instance_defaults`, `projects`, `auth`, `logs`,
`clone`, `reaper`, `usage`, `metrics` — each documented below.

## `claude` — binary & bridge spawn (`ClaudeConfig`)

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

## `instance_defaults` — new-bridge defaults (`InstanceDefaults`)

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `spawn_mode` | `same-dir` \| `worktree` \| `session` | `same-dir` | Default spawn mode for new bridges. `worktree` requires a git repo. |
| `permission_mode` | see below | `default` | Default permission mode for new bridges. |
| `capacity` | int | `32` | Max concurrent bridges (≥1). |

Permission modes: `default`, `plan`, `acceptEdits`, `auto`, `dontAsk`,
`bypassPermissions`. `bypassPermissions` is footgun-gated — see
`projects.<name>.allow_bypass_permissions` below.

## `projects` — per-project map (`ProjectConfig`)

A map of project name → settings. Additive-only; unknown keys ignored.

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `allow_bypass_permissions` | bool | `false` | The **hard ceiling** for the bypassPermissions footgun gate. A project can never be spawned with `--permission-mode bypassPermissions` unless this is set here in `clauster.yml`. The dashboard's per-session typed-confirm is the second layer. |

```yaml
projects:
  my-repo:
    allow_bypass_permissions: true
```

## `auth` — authentication (`AuthConfig`)

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | **Master auth switch.** Must be `true` for password / reverse-proxy auth to actually gate requests. |
| `password_required` | bool | `false` | Require password login. Needs `password_hash`. |
| `password_hash` | str \| null | `null` | argon2id hash from `clauster hash-password`. |
| `reverse_proxy` | object | *(see below)* | Trusted-reverse-proxy auth settings. |
| `allow_unauthenticated_network` | bool | `false` | Explicit opt-out: permit a non-loopback bind **without** enforced auth (e.g. a trusted LAN). `ops._check_auth` downgrades this to a warning. |
| `cookie_secure` | `auto` \| `always` \| `never` | `auto` | Session-cookie `Secure` flag. `auto` = Secure only over https (or a trusted proxy's `X-Forwarded-Proto=https`). |
| `session_max_age_seconds` | int | `604800` | Session lifetime (≥1; default 7 days). |
| `allowed_origins` | list[str] | `[]` | Extra WebSocket / CSRF origins (e.g. the proxy domain). |

### `auth.reverse_proxy` (`ReverseProxyConfig`)

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Enable trusted-reverse-proxy auth. |
| `user_header` | str | `Remote-User` | Header carrying the authenticated user. |
| `shared_secret_header` | str | `X-Proxy-Auth` | Header carrying the HMAC signature. |
| `trusted_ips` | list[str] | `[]` | Peer-IP allowlist for the proxy. |
| `shared_secret` | str \| null | `null` | HMAC key the proxy signs `X-Proxy-Auth` with. |
| `hmac_window_seconds` | int | `60` | Clock-skew / replay window (≥0). |

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

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `bridge_log_max_size_mb` | int | `10` | Per-bridge debug-log rotation size (≥1 MB). |
| `keep_rotated` | int | `5` | Number of rotated log files to keep (≥0). |
| `redact_session_url` | bool | `false` | `false` = hybrid (verbatim on disk, redacted over the WebSocket). `true` redacts the session URL on disk too. |
| `strip_ansi_in_stream` | bool | `true` | Strip ANSI escape sequences from the streamed log. |

## `clone` — project clone/create guards (`CloneConfig`)

Clone URLs are user-supplied and hit the network from the host, so defaults are
strict.

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | Allow cloning/creating projects. |
| `allowed_schemes` | list[str] | `["https", "ssh"]` | Permitted clone URL schemes. |
| `allow_private_hosts` | bool | `false` | Block private/LAN IP targets by default (SSRF guard). |
| `allowed_private_cidrs` | list[str] | `[]` | Targeted LAN opt-in. Each entry is validated as a CIDR at load (a malformed entry fails fast rather than silently never matching). |
| `timeout_seconds` | int | `300` | Clone timeout (≥1). |
| `max_mb` | int | `2048` | Post-clone size cap (≥0; `0` = unlimited). |

## `reaper` — ghost-environment reaper (`ReaperConfig`)

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `ui_enabled` | bool | `false` | Expose the ghost-environment reaper in the **dashboard**. The CLI (`clauster reap-environments`) is always available; this gates only the destructive browser surface. |

## `usage` — per-project cost badge (`UsageConfig`)

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `show_cost` | bool | `true` | Show the per-project cost/token badge. `false` hides the badge **and** skips the `/api/projects/{name}/usage` fetch (privacy / screen-share). |
| `currency` | str | `USD` | Badge **label** only. The price table is USD with no FX conversion: the `$` symbol shows only when this is `"USD"`; any other value renders the (still-USD) figure suffixed `USD`. A forward hook for a future `fx_rate`. |

!!! info "Cost is approximate"
    Token counts are exact (read from transcript `usage`); the dollar figure is
    a ballpark from a hand-maintained USD price table (`usage.py`) that drifts as
    pricing changes — unpriced models count as 0.

## `metrics` — live per-bridge resource metrics (`MetricsConfig`)

A point-in-time sample of the bridge's process tree, shown only while a bridge
runs.

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | Show the metrics line. When `false`, hides it **and** skips both the `/api/projects/{name}/metrics` fetch and the server-side sample. |
| `normalize_cpu` | bool | `false` | Divide summed CPU% by the host core count (0–100% of the machine) instead of the raw across-cores figure (which can exceed 100%). |
| `show_disk` | bool | `true` | Toggle the disk read/write rate portion. |
| `sample_interval_seconds` | float | `0.15` | Two-snapshot sampling window (>0, ≤2.0). Longer is steadier but each fetch blocks a worker thread for that long. |
| `poll_seconds` | float | `4.0` | Dashboard metrics refresh cadence (≥1.0), decoupled from the status poll. |

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
