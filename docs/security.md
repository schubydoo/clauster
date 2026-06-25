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

### Login throttle (brute-force friction)

Failed logins are rate-limited in two layers, returning **`429` with a
`Retry-After`** when blocked:

- **Per-key** — a distinguishable client (its peer IP, or a reverse-proxy-asserted
  user) is locked after 5 failures within 5 minutes.
- **Global backoff** — behind a trusted reverse proxy that asserts *no* user,
  every login shares the proxy's socket IP, so a per-IP lock would lock **everyone**
  out (one attacker DoS-ing all users). In that shared-IP case the per-key lock is
  skipped; instead, once failures across all clients cross a ceiling, attempts must
  wait an exponentially-growing interval (capped). A flood degrades to a delay, not
  a blanket lockout a legitimate user can never get past.

!!! warning "In-process only — not an account-security boundary"
    The throttle counters live in memory: they **reset on restart** and are **not
    shared across workers or replicas**. This is brute-force *friction*, not a
    durable lockout. For an internet-exposed deployment, put Clauster behind a
    fronting **IdP / IAP** (or use the reverse-proxy auth) as the real access
    control — see [Networking](networking.md).

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

## HTTP security headers

A `security_headers` middleware (`app.py`) stamps a set of defence-in-depth
headers on **every** response — including the auth guard's own 401/403/redirect
replies, not just route responses. They layer *behind* the primary
Origin/CSRF gate above; they are belt-and-suspenders, not the access control.

- **Content-Security-Policy** — a per-request, **nonce-gated** policy. The
  baseline is `default-src 'self'`; `script-src` lists a fresh per-request
  `'nonce-<value>'` (a `secrets.token_urlsafe(16)`, never a process-wide
  constant) so only the inline `<script>` blocks carrying the matching
  `nonce="…"` attribute run. `'unsafe-inline'` is **dropped** from `script-src`
  entirely — that is what blocks an injected inline script that lacks the
  nonce — while `'unsafe-eval'` is intentionally **retained** for Alpine.
  `frame-ancestors 'none'` and `object-src 'none'` round out the clickjacking /
  plugin surface. (If the nonce is ever absent on a degraded path the policy
  stays *stricter*, never looser — `'unsafe-inline'` is omitted regardless.)
- **`X-Frame-Options: DENY`** — refuses framing outright (a legacy companion to
  `frame-ancestors 'none'`).
- **`X-Content-Type-Options: nosniff`** — stops MIME-type sniffing.
- **`Referrer-Policy: same-origin`** — deliberately `same-origin`, **not**
  `no-referrer`. Under `no-referrer` a spec-compliant browser serializes the
  `Origin` of a same-origin native `<form>` POST navigation as the literal
  `null`, which the CSRF `Origin` gate then rejects — silently `403`-ing the
  login/logout forms. `same-origin` keeps the real `Origin` on same-origin
  navigations while still suppressing the referrer cross-origin. (Safe only
  while no secret rides a same-origin URL — Clauster credentials are all
  cookie/header-borne.)
- **`Strict-Transport-Security`** — emitted **only over HTTPS** (reusing the
  same secure-cookie detection), so a plain-HTTP LAN deployment never pins a
  browser to a scheme it can't serve. No `includeSubDomains`, to avoid bricking
  a sibling subdomain on a shared parent domain.

## Exposing beyond the LAN

Clauster defaults to a loopback bind and assumes **trusted host-local
infrastructure** — the gates above harden *that* model. But the README pitches
phone/remote use, and many will expose Clauster to a hostile network (Tailscale
Funnel, Cloudflare Tunnel, a public reverse proxy). Host hardening itself — OS
patching, firewalling, and the tunnel/proxy you front with — stays your
responsibility; the notes below are the Clauster-side minimums for that case.

### Require TLS

Never serve the dashboard or its session cookie over plain HTTP off-host.
Terminate TLS at a proxy or tunnel and force the secure cookie with
`auth.cookie_secure: always`, so the cookie can't ride a downgraded request in
the clear. Clauster already warns at startup when password auth is on but the
cookie would likely ship without `Secure` — on a hostile network, treat that
warning as a hard stop.

### Front with an identity-aware proxy

A single shared password is the weakest link for an internet-exposed deployment.
Put an **IdP / IAP** in front — an SSO/forward-auth proxy (Authelia, Authentik,
Cloudflare Access, Pomerium, oauth2-proxy) or a private overlay (Tailscale,
WireGuard) — so a real identity is checked *before* a request reaches Clauster.
The reverse-proxy path (peer-IP allowlist + an HMAC-signed user header,
`auth.reverse_proxy`) is built to trust exactly such a proxy. Once it is your
primary gate, prefer **dropping `password_required` entirely** — the `/login`
route is deliberately exempt from the auth middleware (so a locked-out operator
can always reach it), which means there is *no* Clauster-side option to restrict
it to loopback. If you need to keep a password fallback, the fronting proxy must
block or `403` `POST /login` from non-loopback sources — that is the only
enforcement point.

### The login lockout is friction, not a boundary

The built-in failed-login throttle (per-key lock + a global backoff, returning
`429` + `Retry-After`) raises the cost of brute force, but its counters are
**in-process only** — they reset on restart and aren't shared across workers or
replicas, and behind a shared-IP proxy the global path degrades to a bounded
delay rather than a hard lockout. It is brute-force friction, not an
account-security boundary — the fronting IdP/IAP above is. See
[Login throttle](#login-throttle-brute-force-friction).

### CSRF & sessions on a hostile network

- Add the public origin(s) to `auth.allowed_origins`: the strict `Origin`
  allowlist on unsafe methods (and on WebSocket accept) is the CSRF guard, so an
  origin you don't list is rejected. Set it to your **public URL** (e.g.
  `https://clauster.example.com`), not the internal `host:` bind address — when
  TLS terminates at a proxy, the `Origin` Clauster sees is the public hostname,
  so a bind-address value silently `403`s every request.
- Sessions are signed cookies with server-side revocation — logout bumps a
  persistent epoch, so "log out everywhere" instantly kills a cookie that may
  have leaked. Keep `session_max_age_seconds` short for an exposed deployment.
- `SameSite=Lax` + `Secure` (above) limit cross-site cookie replay; the fronting
  proxy must not strip or forge the `Origin` / forwarded headers Clauster checks.

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

Outbound lifecycle webhooks (`webhooks.py`) are the host's **other** egress
path, so they carry their own opt-in SSRF deny-list. Set
`webhooks.block_private_targets: true` (default `false`) to drop any webhook URL
whose host is — or resolves to — a loopback / link-local / private / CGNAT /
metadata IP, using the **same** private-range classifier as the clone guard
(it imports `provisioning._EXTRA_PRIVATE_NETS`). Default-off preserves the
LAN-receiver use case. See
[`webhooks`](configuration.md#webhooks--outbound-lifecycle-webhooks-webhooksconfig)
in the configuration reference for the field.

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
