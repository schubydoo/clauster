# Security

Clauster's guiding principle is **fail closed, never silently**: auth gates
default to denial, and a configuration that would serve an unauthenticated
dashboard to the network is refused at startup rather than quietly accepted.

For what Clauster keeps on disk — and how to purge it — see
[Privacy & data at rest](privacy.md).

## Authentication (fail-closed)

The auth foundation lives in `auth.py` as pure, FastAPI-free functions (the web
wiring — middleware, routes, cookie handling — lives in `app.py`). It offers
three trust paths:

- **Password login** → a signed-cookie session (`issue_session` /
  `read_session`).
- **Reverse proxy** → peer-IP allowlist + an HMAC-signed header (`peer_trusted`
  / `verify_proxy_hmac`).
- **Cross-site guard** → a strict `Origin` allowlist (`build_allowed_origins` /
  `normalize_origin`).

### The master switch

`auth.enabled` is the master switch. The runtime guard gates on it, so
**`password_required` or `reverse_proxy.enabled` without `auth.enabled: true` is
a silent open door** — the operator sets a password, but the dashboard is still
served to anyone. The config validator refuses that combination on a
non-loopback bind.

### Two startup validators

`ClausterConfig` runs a model validator (`_loopback_or_authed`) that refuses to
start when either of these holds:

1. **Non-loopback bind without enforced auth.** A `host` outside
   `{127.0.0.1, ::1, localhost}` requires `auth.enabled: true` together with
   `password_required` (and a hash) or `reverse_proxy.enabled` — unless you
   explicitly opt out with `auth.allow_unauthenticated_network`.
2. **`password_required` with no `password_hash`.** This would lock everyone out
   (or be silently skipped), so startup is refused with a clear message.

The "is auth actually enforced?" question is answered by a single shared helper
(`_missing_enforced_auth`) so the config validator and the `clauster doctor`
diagnostics agree.

### Passwords

Passwords are hashed with **argon2id**. Generate a hash with:

```sh
clauster hash-password
```

Store the resulting `$argon2id$…` string in `auth.password_hash` (or
`CLAUSTER_AUTH_PASSWORD_HASH`). Verification uses a constant-time path even when
no password is configured / the attempt is empty, to avoid a "no password set"
timing oracle.

### Sessions & cookies

- Sessions are **signed cookies** (`itsdangerous`) with server-side revocation —
  "log out everywhere" bumps a persistent session epoch; cookies issued before
  the bump are rejected even if they have not yet expired. (The signing secret
  itself is constant across logouts.)
- `session_max_age_seconds` defaults to 7 days.
- `cookie_secure` controls the `Secure` flag: `auto` sets it only over https (or
  behind a trusted proxy reporting `X-Forwarded-Proto=https`); `always` forces
  it; `never` disables it.
- Clauster warns at startup when password auth is on but the cookie would likely
  ship **without** `Secure` (plain-http LAN, no TLS proxy). Put Clauster behind
  https / a TLS proxy, or set `auth.cookie_secure: always`.

### WebSockets & origins

WebSocket connections are **authenticated before accept** and origin-checked.
Add the proxy domain or any extra trusted origins to `auth.allowed_origins`.

## Workspace trust

A bridge **refuses to spawn in an untrusted directory**. Trust lives in
`~/.claude.json` under
`projects[<resolved-abs-path>].hasTrustDialogAccepted` and inherits down a tree.
Clauster offers an explicit **"Trust this directory"** action that sets the flag
for exactly one project key; trusted directories show a green shield and start
with no prompt.

The `claude` CLI writes the same file concurrently, so the trust writer
(`trust.py`) guards it with two layers:

- **Atomic replace** (temp file + `os.replace`) so no reader ever sees a
  half-written file, and every key Clauster doesn't touch is preserved.
- **An advisory `flock`** held across the whole read-modify-write (via a sidecar
  `<file>.lock`, never the target itself — `os.replace` swaps the inode). This
  serializes Clauster's own concurrent writers and shrinks the window against
  the CLI to the gap between the read-under-lock and the replace. POSIX only; on
  platforms without `fcntl` it degrades to a best-effort no-op (the atomic
  replace still prevents a torn file).

A one-time `.bak` is taken before the first modification.

## Auto-enable remote control

Before the first spawn, Clauster marks remote control acknowledged
(`hasUsedRemoteControl` / `remoteDialogSeen`) in the runtime user's
`~/.claude.json`. Without this a detached-stdin bridge would block forever on the
one-time interactive "Enable Remote Control? (y/n)" prompt it can never answer.
On by default (`claude.auto_enable_remote_control`); set `false` to manage the
flags yourself.

## Log redaction

The bridge debug log is streamed over a WebSocket and sanitized line by line
(`redact.py`). Redaction always runs against an **ANSI-stripped** view, so escape
sequences can't split an identifier and smuggle a secret past the
word-boundary-anchored regexes.

Three layers:

1. **ID redaction (primary guarantee).** Masks `env_` / `session_` / `cse_`
   identifiers (the prefix is kept readable) — these act as bearer-equivalent
   credentials in a URL — and **bare UUIDs** (account / instance identifiers the
   bridge prints in full; not bearer credentials, but kept off the stream).
2. **Secret-shape redaction (defense-in-depth).** A conservative allow-list of
   obvious secret shapes — GitHub tokens (`ghp_`/`gho_`/… , `github_pat_`),
   GitLab PATs (`glpat-`), AWS access-key IDs (`AKIA…`), OpenAI/Anthropic-style
   `sk-…`, Slack `xox[baprs]-…`, and `Authorization: Bearer …` headers.
3. The bridge's own `[REDACTED]` output for most secrets — never relied on alone.

!!! warning "Known limitation (by design)"
    The secret-shape layer is a **shape allow-list** anchored on word
    boundaries. It will **not** catch a novel/unstructured high-entropy secret —
    a bearer value that isn't literally `Bearer …`, a raw JWT, or a vendor token
    whose prefix isn't listed. That is acceptable because it is
    defense-in-depth: the primary WebSocket guarantee is the
    `env_`/`session_`/`cse_` + UUID redaction. Add new shapes as they appear
    rather than assuming coverage.

### Hybrid by default

Redaction is **hybrid** by default: the on-disk log keeps IDs verbatim (for local
debugging), and only the WebSocket stream is redacted. Set
`logs.redact_session_url: true` to redact the session URL on disk too. ANSI
stripping in the stream is controlled by `logs.strip_ansi_in_stream` (default
on).

## Clone / SSRF guards

Project clone URLs are user-supplied and hit the network from the host, so
`clone` defaults are strict: only `https` / `ssh` schemes, private/LAN IP targets
blocked by default (`allow_private_hosts: false`), a size cap, and a timeout.
Targeted LAN access is an explicit `allowed_private_cidrs` opt-in — each entry is
validated as a CIDR at load so a malformed allow-list entry fails fast instead of
silently never matching.

## bypassPermissions footgun gate

A bridge can never be spawned with `--permission-mode bypassPermissions` unless
the project sets `allow_bypass_permissions: true` in `clauster.yml` (the hard
ceiling). The dashboard's per-session typed-confirm is the second layer.

## Ghost-environment reaper

The reaper (`clauster reap-environments`) defaults to a **dry run** and fails
closed: if it cannot enumerate the live bridge set it aborts rather than risk
archiving a still-live environment. The destructive **dashboard** surface is off
by default and gated by `reaper.ui_enabled`; archive is reversible, force-delete
requires typing `DELETE`.
