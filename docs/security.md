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
- **Bearer token** → `Authorization: Bearer …`, checked against
  `auth.api_token_hash` or the `api_tokens` table (`verify_token`).
- **Reverse proxy** → peer-IP allowlist + an HMAC-signed header (`peer_trusted`
  / `verify_proxy_hmac`).
- **Cross-site guard** → a strict `Origin` allowlist (`build_allowed_origins` /
  `normalize_origin`). Unlike the credential paths above, this one is **not**
  gated on `auth.enabled` — see below.

### The master switch

`auth.enabled` is the master switch **for the credential paths**. The runtime
guard gates them on it, so **`password_required` or `reverse_proxy.enabled`
without `auth.enabled: true` is a silent open door** — the operator sets a
password, but the dashboard is still served to anyone. The config validator
refuses that combination on a non-loopback bind.

**The cross-site `Origin` guard is deliberately outside that switch.** It is a
CSRF/WS-hijack defence, not an authentication method: a browser page the operator
visits can reach a loopback-bound service regardless of whether a password is
set, so the allowlist is enforced on every unsafe method and every WebSocket
handshake **even when `auth.enabled` is false**. With auth off there is no
credential to exempt, so the gate rejects only a *present* `Origin` that isn't
allowlisted — an absent `Origin` (a CLI/script client, never a browser) still
passes.

`build_allowed_origins` auto-allows `127.0.0.1`/`localhost`/`[::1]` **at
`config.port`** for a loopback bind, and *nothing* for a non-loopback bind — so the
default deployment needs no configuration. Note what the auto-allow keys on: the
**bind host**, not the address the browser used. So a non-loopback bind rejects even
`http://localhost:<port>` — a published Docker port (the image binds `0.0.0.0`) is
the common case — and must list its browser-facing origin in `auth.allowed_origins`.
A loopback bind needs the entry too whenever the browser arrives by some other route:
a reverse proxy or tunnel (the `Origin` is the public hostname), or an SSH
port-forward onto a *different* local port (`-L 9000:localhost:7621` →
`http://localhost:9000`, port mismatch). With auth enabled this was already true; the
change extends the same requirement to auth-off deployments, which previously skipped
the check entirely. It fails closed and visibly — a rejected write with
`origin check failed`, or a WS that won't connect.

### Two startup validators

`ClausterConfig` runs a model validator (`_loopback_or_authed`) that refuses to
start when either of these holds:

1. **Non-loopback bind without enforced auth.** A `host` outside
   `{127.0.0.1, ::1, localhost}` requires `auth.enabled: true` together with
   `password_required` (and a hash), an `api_token_hash`, or `reverse_proxy.enabled` — unless you
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

WebSocket connections are **authenticated before accept** and origin-checked —
and the origin check runs **even when `auth.enabled` is `false`** (see
[The master switch](#the-master-switch)), because a cross-site page can open a
socket to a loopback service regardless of whether a password is set.
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
  nonce — and `'unsafe-eval'` is **dropped** too: Clauster ships the
  CSP-friendly `@alpinejs/csp` build (`alpine.csp.min.js`, `'self'`-allowed), so
  Alpine no longer needs a `new Function()` evaluator. `frame-ancestors 'none'`
  and `object-src 'none'` round out the clickjacking / plugin surface.
  `style-src` is **nonce-gated the same way**: its inline `<style>` blocks carry
  the matching nonce and `'unsafe-inline'` is dropped from it too — the only
  constraint that leaves is that Alpine `:style` bindings must use the object
  form. (If the nonce is ever absent on a degraded path the policy stays
  *stricter*, never looser — `'unsafe-inline'` is omitted from both regardless.)
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
`auth.reverse_proxy`) is built to trust exactly such a proxy. The proxy's
identity check is **admission, not access control**: Clauster is single-operator
by design, so whoever the IdP admits acts with the one operator's full host
control, and the config-write audit trail attributes actions to the constant
`admin` actor rather than the IdP identity. The IdP group is an on/off switch
for the whole host — it should contain exactly one person. Once it is your
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
`~/.claude.json` under `projects[<resolved-abs-path>].hasTrustDialogAccepted`. A
**git repository requires its own** trust grant: Claude Code 2.1.232 stopped
honoring a parent directory's grant for nested git repos, so a projects-root grant
no longer cascades to the repos under it. A non-repo directory still inherits from a
trusted ancestor. Clauster mirrors this exactly, so the dashboard never shows a repo
as trusted that the CLI would reject at spawn — it fails **closed**.

Two trust actions, both setting the flag for the project's own key:

- **Trust & start** — launching a session in an untrusted directory shows a
  confirm dialog (with a safety checkbox) that trusts the directory, then spawns.
- **Trust all** — a dashboard banner, shown while any discovered project is
  untrusted, grants every discovered project its own key at once. It exists to
  reconcile an install after the 2.1.232 change, where repos trusted only via a
  parent grant now read untrusted.

Trusted directories show a green shield and start with no prompt.

The `claude` CLI writes the same file concurrently, so the trust writer
(`trust.py`) guards it with two layers:

- **Atomic replace** (temp file + `os.replace`) so no reader ever sees a
  half-written file, and every key Clauster doesn't touch is preserved.
- **An advisory `flock`** held across the whole read-modify-write, taken on a lock
  file under `<state_dir>/locks/` keyed by the target's real path — never the
  target itself (`os.replace` swaps the inode) and never a `<file>.lock` sidecar
  beside it, which for a project `.claude/settings.json` would land inside your
  git-tracked tree. This serializes Clauster's own concurrent writers and shrinks
  the window against the CLI to the gap between the read-under-lock and the
  replace. POSIX only; on platforms without `fcntl` it degrades to a best-effort
  no-op (the atomic replace still prevents a torn file). Because the lock file is
  keyed under one deployment's `state_dir`, the cross-process guarantee covers a
  single Clauster deployment — the same scope as the config and `CLAUDE.md`
  writers, which share this lock.

A one-time `.bak` is taken before the first modification.

> **Upgrading from 1.0.2 or earlier?** Those versions wrote the sidecar beside the
> target, so you may still have a 0-byte `settings.json.lock` / `.mcp.json.lock` /
> `.claude.json.lock` on disk. Clauster no longer creates or uses them and
> deliberately does not delete them — a file inside your project is yours, and
> unlinking a lock file is the very race the pattern avoids. Remove them by hand
> (and, if you committed one, `git rm` it) once no older Clauster is running.

## Auto-enable remote control

Before the first spawn, Clauster marks remote control acknowledged
(`hasUsedRemoteControl` / `remoteDialogSeen`) in the runtime user's
`~/.claude.json`. Without this a detached-stdin bridge would block forever on the
one-time interactive "Enable Remote Control? (y/n)" prompt it can never answer.
On by default (`claude.auto_enable_remote_control`); set `false` to manage the
flags yourself.

## Log redaction

The bridge debug log is streamed over a WebSocket and sanitized line by line
(`redact.py`). Redaction runs against the view a browser **renders**. That view
has the escape sequences removed, including the `OSC`, `DCS`, `SOS`, `PM` and
`APC` string sequences and their payloads. It also has the invisible control
characters removed, and the invisible Unicode characters a browser draws nothing
for: zero-width spaces and joiners, the bidirectional controls, the variation
selectors, the soft hyphen, the byte-order mark and the other code points in the
Unicode `Default_Ignorable_Code_Point` set. None of these can therefore split an
identifier into fragments too short for the word-boundary-anchored regexes. A
side effect is that these characters are deleted from every redacted view this
code produces, not only the streamed log. `sanitize_line` also redacts each
transcript turn (the usage view) and the streamed hosted-agent assistant text,
and `redact_for_disk` redacts `instance.error_detail`, clone-job errors and agent
result text. Some of those surfaces are conversational prose, so the effect is
visible there: a joiner inside an emoji sequence (a family emoji) renders as its
separate glyphs, and a bidirectional mark in an Arabic or Hebrew turn is dropped.
Each is a redacted view, not the verbatim on-disk log, so this is accepted:
redaction wins over character fidelity in a redacted copy.

Removing those bytes can also *delete* a word boundary. `user<ESC>[32menv_<ULID>`
renders as `userenv_<ULID>`, which reads exactly like ordinary compound text such
as `userenv_production`. Clauster therefore records where each removal happened
and re-tries the same masks anchored at those positions. Tab, carriage return and
newline are never removed. They are visible separators, and the line structure
depends on them.

Three layers:

1. **ID redaction (primary guarantee).** Masks `env_` / `session_` / `cse_`
   identifiers (the prefix is kept readable) — these act as bearer-equivalent
   credentials in a URL — and **bare UUIDs** (account / instance identifiers the
   bridge prints in full; not bearer credentials, but kept off the stream). The
   live pty-screen view masks these to the neutral `<redacted>` token instead,
   with no readable prefix. The `<` the token adds is itself a word boundary, so
   it closes an identifier welded to another identifier.
2. **Secret-shape redaction (defense-in-depth).** A conservative allow-list of
   obvious secret shapes — GitHub tokens (`ghp_`/`gho_`/… , `github_pat_`),
   Clauster API tokens (`clauster_pat_…`), GitLab PATs (`glpat-`), AWS access-key
   IDs (`AKIA…`), OpenAI/Anthropic-style
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

!!! warning "Bounded scope: an identifier written mid-token stays visible"
    A mask needs a word boundary before the identifier, or the position of a
    removed escape. An identifier whose preceding characters were written as
    plain text (`xyzenv_<ULID>`) has neither, so it is not masked. That is a
    different threat: producing it means already controlling the whole line, and
    an attacker who can print arbitrary text beside an identifier does not need
    to smuggle it past the mask. The case that matters is the escape weld, where
    Clauster's own bridge prints the real identifier and an injected escape only
    deletes the boundary. This bounded scope is for the streamed log and the
    on-disk mirror. A terminal has already discarded the escape on the live
    pty-screen view. So that view also masks a real identifier (the `01` shape)
    welded onto the word before it, and an id welded to another id. An ordinary
    compound name such as `resolve_session_transcript` stays readable. A welded
    secret, and a welded id without the `01` shape, stay visible there.

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
[`webhooks`](reference/config.md#webhooks-outbound-lifecycle-webhooks-webhooksconfig)
in the configuration reference for the field.

## bypassPermissions footgun gate

A bridge can never be spawned with `--permission-mode bypassPermissions` unless
the project sets `allow_bypass_permissions: true` in `clauster.yml` (the hard
ceiling). The dashboard's per-session typed-confirm is the second layer.

The `inherit` permission mode (**No forced mode** in the launch picker) cannot itself
*request* `bypassPermissions`, and Clauster refuses to write that value into any
`settings.json` it manages. **But the caveat is structural:** because no
`--permission-mode` flag is passed, an existing `permissions.defaultMode` in the
project, local, or user settings file takes effect — including a `bypassPermissions`
one that was hand-edited, committed to the repo, or enterprise-managed, which Clauster
cannot prevent. Before `inherit` existed, the always-passed flag overrode any such
file; with `inherit`, that file wins. On a project with
`allow_bypass_permissions: false`, prefer an explicit mode — or audit the project's
settings files before offering `inherit` to its operators. The mode is still screened
against the allowed set before any spawn, so no unrecognized string ever reaches the
subprocess, and `inherit` is not a value this deployment can write into
`permissions.defaultMode`: it is a Clauster launch-time sentinel, not a claude mode.

## Ghost-environment reaper

The reaper (`clauster reap-environments`) defaults to a **dry run** and fails
closed: if it cannot enumerate the live bridge set it aborts rather than risk
archiving a still-live environment. The destructive **dashboard** surface is off
by default and gated by `reaper.ui_enabled`; archive is reversible, force-delete
requires typing `DELETE`.

It is also **scoped to this instance's `projects_root`**. The environment list is
account-wide while the liveness check is instance-scoped — it sees only this OS
user's sessions and this instance's projects — so an environment outside
`projects_root` is unattributable and never reaped. Without that rail, reaping on
one instance could archive another instance's *live* environment and tear down
its running session. See
[`clauster reap-environments`](reference/cli.md#clauster-reap-environments-archive-ghost-environments)
for the scope rules, including the shared-`projects_root` caveat.
