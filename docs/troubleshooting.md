# Troubleshooting

Symptom-first fixes for an install that is not behaving. Every section follows
the same shape: what you see (with the verbatim error string, so search finds
it), the most likely cause, the command to run, and a link to the page that
explains the mechanism.

## Start here — three commands

```sh
clauster doctor                        # config, claude binary, login, trust,
                                       # state_dir, port — explained below
curl -s http://127.0.0.1:7621/healthz  # liveness (default port 7621)
journalctl -u clauster -n 100          # server log — or: docker logs <name>
```

Bridge debug logs live under `<state_dir>/logs/` (default `~/.clauster/logs/`),
one file per spawn named `<label>-<timestamp>-<seq>.log`. What `doctor` and
`/healthz` report is described under
[Health checks](operations.md#health-checks); how to read a bridge log is in
[Reading the bridge debug log](operations.md#reading-the-bridge-debug-log).

## The dashboard won't load

### "connection refused" / the page just times out

**What you see:** the browser reports `ERR_CONNECTION_REFUSED` or times out;
`curl` says `Connection refused`.

**Most likely cause:** Clauster is not running, crashed at startup (see
[It won't start at all](#it-wont-start-at-all)), or is on a different port
than the one you are hitting (default `7621`).

```sh
systemctl status clauster        # is it running? what did it last print?
curl -s http://127.0.0.1:7621/healthz   # from the host itself
ss -tlnp | grep 7621             # is anything listening on the port?
```

**Mechanism:** [Networking — loopback vs
non-loopback](networking.md#loopback-vs-non-loopback).

### The page loads locally but not from another machine

**What you see:** `curl http://127.0.0.1:7621/healthz` works on the host, but
the same port from your laptop or phone refuses to connect.

**Most likely cause:** Clauster binds `127.0.0.1` by default, so only the host
itself can reach it. Binding a LAN address (`host: 0.0.0.0`) requires enforced
auth — see the "refusing non-loopback host" refusal under
[I can't get in](#i-cant-get-in) — or a reverse proxy in front of the
loopback bind.

**Mechanism:** [The auth / networking
matrix](networking.md#the-auth-networking-matrix).

### The container exits immediately after `docker run`

**What you see** (in `docker logs <name>`): one of

```text
exec /usr/local/bin/claude: no such file or directory
claude: not found
```

**Most likely cause:** the official image is Alpine-based (musl libc), and you
bind-mounted a **glibc** build of `claude` from the host into the container.
The file is present, but the glibc loader it needs is not, so exec fails with
a misleading "no such file or directory".

**The fix:** mount a musl build of `claude` instead, or derive your own image
that installs one (the installer needs `bash`):

```sh
apk add --no-cache bash curl && curl -fsSL https://claude.ai/install.sh | bash
```

A container can also exit immediately because of a config refusal — a
non-loopback bind without enforced auth, or `password_required` without a
hash. Check `docker logs` for the strings in
[I can't get in](#i-cant-get-in).

**Mechanism:** [Installation — Docker](installation.md#docker) ·
[Networking — Docker](networking.md#docker).

## I can't get in

### Buttons are refused with `origin check failed`, or a live view won't connect

**What you see:** the dashboard loads and reads fine, but any action (start a
session, trust a folder, restart) returns `403 {"detail": "origin check
failed"}`, and/or the live log / terminal view never connects (the WebSocket
closes with code `1008`).

**Most likely cause:** the browser origin you use is not in the `Origin`
allowlist. Clauster auto-trusts `127.0.0.1`/`localhost`/`[::1]` **only at
`config.port`, and only when `host:` is itself a loopback address**. A
non-loopback bind — `0.0.0.0`, a LAN IP, or the Docker image (which binds
`0.0.0.0`) — auto-trusts *nothing*, so even browsing it at
`http://localhost:7621` is rejected. A tunnel, reverse proxy, or an SSH
port-forward onto a *different* local port has the same effect on a loopback
bind.

**Fix:** list the address exactly as it appears in the browser's URL bar:

```yaml
auth:
  allowed_origins: ["http://localhost:7621"]   # scheme + host + port
```

This applies whether or not a password is set — the origin allowlist is a
cross-site defence, not an authentication method, so it is enforced even when
`auth.enabled` is `false`.

**Mechanism:** [The master switch](security.md#the-master-switch) ·
[Upgrading](upgrading.md).

### I get a 403 after putting it behind nginx / Caddy / Traefik

**What you see:** the proxy reaches Clauster, but requests are denied with
`403`.

**Most likely cause:** trusted-header (forward-auth) mode is misconfigured —
the request does not arrive from an IP in the trusted-proxy list, or the
identity header the proxy is supposed to inject is missing or stripped.
Test directly against the Clauster port (bypassing the proxy) to isolate
which side is denying.

**Mechanism:** [Behind a reverse proxy](networking.md#behind-a-reverse-proxy)
· [Trusted-header (forward-auth)
mode](networking.md#trusted-header-forward-auth-mode).

### Login redirects in a loop / the session cookie doesn't stick

**What you see:** the password is accepted but you land back on the login
page; every click bounces you to `/login`.

**Most likely cause:** the signed session cookie never makes it back —
a proxy stripping `Cookie`/`Set-Cookie`, the browser blocking the cookie, or
mixing origins (reaching the same instance via both `http://` and `https://`
or via two hostnames). Try a private-browsing window directly against the
Clauster port; if that works, the proxy layer is eating the cookie.

**Mechanism:** [Behind a reverse proxy](networking.md#behind-a-reverse-proxy)
· [Security — sessions & cookies](security.md#sessions-cookies).

### "password_required but no password_hash set"

**What you see:** `clauster doctor` fails the `auth` row with
`password_required but no password_hash set`, and startup refuses with:

```text
auth.password_required is set but auth.password_hash is empty. Generate one
with `clauster hash-password` (or set CLAUSTER_AUTH_PASSWORD_HASH).
```

**Most likely cause:** password login is enabled but no hash is configured.
Clauster fails closed rather than serving a login form nobody can pass.

```sh
clauster hash-password    # prints the hash to paste into auth.password_hash
```

**Mechanism:** [Security — passwords](security.md#passwords) ·
[`auth` config reference](reference/config.md#auth-authentication-authconfig).

### "refusing non-loopback host ... without enforced auth" — it refuses to start

**What you see:** startup aborts (or `doctor` fails the `auth` row with
`non-loopback host <x> without enforced auth`):

```text
refusing non-loopback host='0.0.0.0' without enforced auth. Set auth.enabled:
true together with auth.password_required (+ a hash from `clauster
hash-password`), auth.reverse_proxy.enabled, or auth.api_token_hash (from
`clauster hash-token`) — or, to opt out on a trusted LAN,
auth.allow_unauthenticated_network.
```

**Most likely cause:** you changed `host` to a LAN/all-interfaces address
without configuring an auth method — deliberate fail-closed behavior, since
an open dashboard on a network port hands out control of your machine. Follow
the message: enable one of the listed auth methods, or explicitly opt out
with `auth.allow_unauthenticated_network` on a trusted network.

**Mechanism:** [Authentication
(fail-closed)](security.md#authentication-fail-closed) · [Password auth on a
non-loopback bind](networking.md#password-auth-on-a-non-loopback-bind).

## It won't start at all

### "address already in use" — port already taken

**What you see:** startup dies with an OS-level `address already in use`
error; `clauster doctor` warns on the `port` row:
`7621 already in use (Clauster already running?)`.

**Most likely cause:** another Clauster (often a still-running service unit)
or an unrelated process owns the port.

```sh
ss -tlnp | grep 7621     # who owns it?
```

Stop the other process, or change `port` in `clauster.yml`.

### "not writable" / permission denied writing state

**What you see:** `doctor` fails the `state_dir` row with
`<state_dir> not writable: ...` or
`<state_dir> can't be created: <ancestor> not writable`.

**Most likely cause:** the user running Clauster (often a dedicated service
user under systemd) does not own the state directory (default `~/.clauster`).

```sh
ls -ld ~/.clauster       # as the service user — who owns it?
```

Fix ownership, or point `state_dir` somewhere the service user can write.

**Mechanism:** [Run as a systemd
service](installation.md#run-as-a-systemd-service-linux).

### "no config found" / "invalid config"

**What you see:** `doctor` fails the `config` row with one of three details —
`no config found: ...`, `invalid config: ...`, or `config is not valid YAML: ...`
— and most other verbs exit `2` with a one-line `clauster: config error:`
message. Three behave differently: `doctor` itself exits `1` with the table
above, `clauster run` with no config at all opens the setup wizard rather than
failing, and `install-service` still renders a unit using defaults.

**Most likely cause:** no `clauster.yml` where Clauster looks for one, or a
mistake inside it. The three details separate the cases, which is what tells you
where to look:

| Detail | What is wrong | Where the message points |
| --- | --- | --- |
| `no config found` | No file at any searched path | The search order |
| `config is not valid YAML` | The file does not parse — a stray tab, an unclosed quote | A line and column |
| `invalid config` | The file parses but a value is rejected | The offending key — or, for a root that is not a mapping, only the file |

```sh
clauster doctor -c /path/to/clauster.yml   # same file the service uses
```

**Mechanism:** [Configuration — loading &
overrides](guides/configuration.md#loading-overrides).

### "database migration failed; refusing to start"

**What you see** (server log):

```text
database migration to head failed; refusing to start: ...
```

followed by a `database migration failed: ...` error.

**Most likely cause:** the state database is ahead of the binary (it was
migrated by a newer build and you downgraded), or the file is corrupted.
Clauster refuses to run against a schema it does not understand rather than
guessing.

**The fix:** upgrade back to (at least) the version that migrated the DB, or
restore a backup — see [Recovering from a corrupted state
database](operations.md#recovering-from-a-corrupted-state-database) and
[upgrading.md](upgrading.md).

## A bridge won't start, or dies right after starting

### The card sits on "starting" then goes to "error"

**What you see:** the project card shows `starting`, then flips to `error`.

**Most likely cause:** the bridge spawned but never registered an environment
within `startup_grace_seconds` — most often a `claude` login problem (the
bridge starts, then hangs unauthenticated).

```sh
clauster doctor                          # read the claude-login row
tail ~/.clauster/logs/<label>-*.log      # the bridge's own account of it
```

**Mechanism:** [Reading the bridge debug
log](operations.md#reading-the-bridge-debug-log) · [Bridge
lifecycle](architecture.md#bridge-lifecycle).

### The card goes straight to "crashed"

**What you see:** the card shows `crashed` shortly after spawn.

**Most likely cause:** the `claude` process exited unexpectedly. The bridge
logs its failure reason to its debug file before exiting, and a spawn that
fails outright also captures a stderr tail — so the card's log view (or the
on-disk log) usually names the cause.

### "spawn of ... failed to launch"

**What you see** (server log): `spawn of <name> failed to launch: ...`
(or `pty spawn of <name> failed to launch: ...` for an Interactive Session).

**Most likely cause:** the `claude` process could not be started at all —
a bad binary path, a permissions problem, or a PATH issue (next section).
The tail of the message is the underlying OS error; start there.

### "claude binary 'claude' not found on PATH; Clauster cannot start."

**What you see:** exactly that string, from `clauster run` or `doctor`'s
`claude` row.

**Most likely cause:** Clauster runs in an environment whose `PATH` does not
include `claude` — classically a systemd unit (minimal `PATH`) with `claude`
installed per-user or via nvm-managed node.

**The fix**, in order of preference:

1. Set `claude.binary` in `clauster.yml` to the absolute path of the binary.
2. Add its directory to the service unit's `PATH` (see the systemd notes in
   [Installation](installation.md#run-as-a-systemd-service-linux)).
3. For nvm-managed node tooling in spawned bridges, see `claude.path_append`
   and `claude.node_from_nvm` in the
   [`claude` config reference](reference/config.md#claude-binary-bridge-spawn-claudeconfig)
   and [npx/node MCP servers under
   systemd](reference/config.md#npxnode-mcp-servers-under-systemd-nvm).

### The trust prompt keeps blocking the spawn

**What you see:** a bridge spawns but sits waiting on Claude's workspace-trust
prompt instead of becoming ready.

**Most likely cause:** the git repository needs its **own** trust grant. Since
Claude Code 2.1.232 a parent directory's grant no longer covers nested git repos,
so trusting `projects_root` does not trust the repos under it (a non-repo directory
still inherits from a trusted ancestor).

**Fix:** launch it once via **Trust & start** (which trusts, then spawns), or click
**Trust all** on the dashboard to grant every discovered project its own key at once.

**Mechanism:** [Security — workspace trust](security.md#workspace-trust).

## Claude itself is the problem

### "not logged in" / "access token has expired; re-authenticate with `claude`"

**What you see:** `doctor` warns on the `claude-login` row —
``not logged in (... missing) — run `claude` to authenticate; bridges inherit
the operator's login`` or ``logged out (no access token in ...) — re-run
`claude` `` — or an operation fails with
``access token has expired; re-authenticate with `claude` `` or
`no claudeAiOauth.accessToken in ...`.

**Most likely cause:** the account that runs Clauster has no usable `claude`
credentials. Bridges inherit the *operator's* login, so a bridge can spawn
and then hang unauthenticated.

**The fix:** run `claude` interactively **as the service user** and complete
login, or use the dashboard login flow
([`login_shepherd`](reference/config.md#login_shepherd-dashboard-driven-claude-account-login-loginshepherdconfig)).
An `ANTHROPIC_API_KEY` in the service environment also satisfies the check.

### "a login flow is already in progress; cancel it first"

**What you see:** the dashboard sign-in panel refuses to start, reporting that a
login flow is already in progress.

**Most likely cause:** an earlier sign-in was started but its authorize link was
never used. The shepherd runs one flow at a time, so the abandoned one holds the
slot.

**The fix:** reload the dashboard. The sign-in panel reads the server's state on
load, so it re-opens on the in-progress flow with its authorize link and a
**Cancel** button — cancel, then start again. A flow untouched for more than 15
minutes is also reclaimed automatically by the next sign-in attempt.

### The claude version is too old (min_version)

**What you see:** `doctor` fails the `claude` row with
``<version> < required <min_version> — run `claude update` (or reinstall via
the installer) to reach >= <min_version>``.

**The fix:** `claude update` (as the same user Clauster runs as), then re-run
`clauster doctor`.

## An Interactive Session (pty) bridge is wedged

### The live log tail is silent or frozen

**What you see:** the dashboard's live log view stops updating.

**Most likely cause:** the WebSocket stream died, not the bridge. Check
whether the on-disk log is still growing:

```sh
tail -f ~/.clauster/logs/<label>-*.log
```

If it grows, the bridge is fine — reload the dashboard to reconnect the
stream. If it is silent too, treat it as a wedged bridge: stop and resume it
from the card.

### The bridge survived a restart and I can't stop it

**What you see:** after a Clauster restart there is a `claude` process (and
its keeper) still running with no project card that controls it.

**Most likely cause:** pty (Interactive Session) bridges are designed to
survive a `systemctl restart` under the shipped unit's `KillMode=process`;
normally Clauster reattaches to them at startup. One that was orphaned
instead is cleaned up with:

```sh
clauster keepers               # list orphaned pty keepers (no project card)
clauster keepers --kill <pid>  # stop one by its keeper PID
```

**Mechanism:** [`clauster keepers`](reference/cli.md#clauster-keepers-stop-an-orphaned-pty-keeper)
· [the `KillMode` / restart caveat](operations.md#restart).

### Is the bridge dead, or is the stream dead? — how to tell

Three probes, from cheapest to most direct:

1. **On-disk log growing?** (`tail -f` as above) — growing means the bridge is
   alive and only the dashboard stream is stale; reload the page.
2. **Card status** — `crashed`/`error` mean the bridge is gone (the status
   table is in
   [Reading the bridge debug log](operations.md#reading-the-bridge-debug-log)).
3. **Process there?** `pgrep -a -f 'claude.*remote-control'` — a live process
   with a dead card usually means an orphaned keeper (previous section).

### "Connect link unavailable — this session's screen could not be read."

**What you see:** a green **Running** Interactive Session card with no **Open in
Claude** button and no QR button, carrying that sentence where the buttons
would be.

**The bridge is fine.** Only its deep link is missing. The session runs, the
live log tail works, and Stop and the live terminal all behave normally.

**Cause:** the keeper reads the connect link off a terminal screen it rebuilds
from the bridge's output. Two faults stop it. The first is a rare byte sequence, a
double-width character left half-overwritten, that makes one frame fail to
render. The keeper then skips the bad chunk and retries on the next one, which
normally succeeds within the 30-second capture window. The second is a failure
that disables the screen emulator for the rest of the session. This message
means capture never succeeded.

**Where it happens:** Windows always, and Linux or macOS only with
`claude.pty_screen_enabled: true`. The keeper builds that screen unconditionally
on Windows, because a Windows console fragments the link across the raw byte
stream. Elsewhere it builds one only for the live-terminal view, and without a
screen there is no screen fault to report.

**The fix:** open the session from
[claude.ai/code](https://claude.ai/code) and pick it from your session list, or
Stop the card and start a new session. Nothing needs repairing on the host.

**Mechanism:** the keeper writes the reason into its sidecar next to the
bridge's log, and Clauster lifts it onto the card. The exact fault is also
logged, one line beginning `clauster.pty_keeper:`:

```sh
grep clauster.pty_keeper ~/.clauster/logs/<label>-*.keeper.log
```

## Everything looks fine but the session never responds

### Hosted / Direct Session: "cannot connect to claustrum"

**What you see:** a Direct Session fails with
`cannot connect to claustrum at <state_dir>/claustrum/daemon.sock`, or the
API returns `hosted channel unavailable: claustrum daemon not connected`.

**Most likely cause:** the claustrum daemon is not running (or not installed —
it is an optional dependency), or its socket path is wrong.

```sh
curl -s http://127.0.0.1:7621/healthz      # the claustrum block names the reason
clauster deps list                         # is claustrum installed?
tail ~/.clauster/claustrum/daemon.log      # what did the daemon last say?
```

Start with `/healthz`: its `claustrum` block carries an `error` describing why
the last start attempt failed — including the commonest case, the binary not
being found.

Authenticate that request on a deploy with `auth.enabled` — an unauthenticated
caller gets liveness only, with no `claustrum` block at all. The block is also
absent when `claustrum.enabled` is false. See
[Health checks](operations.md#health-checks) for what `/healthz` returns to whom.

**Mechanism:** [`claustrum` config
reference](reference/config.md#claustrum-hosted-live-view-channel-claustrumconfig).

### "claustrum daemon unavailable at startup"

**What you see** (server log): `claustrum daemon unavailable at startup: ...`
— Clauster keeps running, but Direct Sessions are unavailable. Related
messages: `running claustrum daemon rejected the persisted token` and
`claustrum daemon connection lost`.

**Most likely cause:** the daemon failed to start or an old daemon is running
with a token Clauster no longer holds. `/healthz` reflects claustrum health;
restarting Clauster retries the connect-or-spawn cycle.

### Reattach failed after a restart

**What you see:** after a Clauster restart, a previously live Direct Session
card shows an error like `reattach failed: ...` (server log:
`hosted: reattach of <id> failed: ...`).

**Most likely cause:** the underlying daemon process ended while Clauster was
down, so there was nothing to reattach to. The transcript is not lost — start
a new Direct Session for the project and resume from the captured session.

### "output lost (N frames)" after a restart

**What you see:** a reattached Direct Session's view opens with a yellow
`output lost (N frames)` marker ahead of the replayed output (server log:
`hosted: daemon replay buffer evicted frames <first>-<last> for process <id>`).

**Most likely cause:** the agent produced more output while Clauster was down
than the claustrum daemon's per-process replay buffer holds, so the daemon
dropped the oldest frames before Clauster could read them. The marker is
deliberate — those frames are gone from the daemon and Clauster will not
pretend the stream is continuous. The conversation itself is unaffected: the
session keeps running and `claude` has still written its own transcript to
disk. Restarting Clauster promptly, or keeping sessions shorter, narrows the
window.

## Getting more detail

### Raising the log level

> **New in 1.1:** the `log_level` key and its `CLAUSTER_LOG_LEVEL` override.
> Before that the server logged at `info` in every deployment.

Set `log_level` (default `info`) to raise the server's own verbosity — it
covers Clauster's loggers and uvicorn's alike:

```yaml
log_level: debug   # debug | info | warning | error
```

For a one-shot run, or in a container or unit file, use the env override
instead of editing the config:

```bash
CLAUSTER_LOG_LEVEL=debug clauster run -c clauster.yml
```

Either way the level is read **once at startup**, so restart Clauster after
changing it. What `debug` mostly adds is **uvicorn's own** debug records —
per-request and per-connection detail — plus a handful of Clauster's
best-effort cleanup diagnostics (a stop signal that was a no-op, a worktree
unlock that failed) and claustrum daemon lifecycle notes. It is a wider view of
the same events, not a separate decision trace.

Treat it as a diagnostic setting, not a steady state: it is high-volume, and
although every line still goes through the same redaction as `info` (see
[log redaction](security.md#log-redaction)), it records much more about what
the server is doing — so treat a `debug` log as sensitive and skim it before
attaching it to an issue.

Note the `logs.*` config keys are a different thing entirely: they control the
**bridge debug log** stream (rotation, ANSI stripping, redaction), not server
verbosity.

### Reading the bridge debug log (on-disk vs the redacted stream)

The dashboard's live tail is ANSI-stripped and redacted; the on-disk file
under `<state_dir>/logs/` is, by default, the **verbatim** debug log
(`logs.redact_session_url: false` — redaction happens only over the
WebSocket). Full walkthrough:
[Reading the bridge debug log](operations.md#reading-the-bridge-debug-log) ·
[`logs` config
reference](reference/config.md#logs-bridge-log-rotation-redaction-logsconfig) ·
[how redaction works](security.md#log-redaction).

### What `clauster doctor` checks actually mean

Doctor's output has two kinds of rows. The table lists the **core rows**,
present in every run:

| Row | OK means | A warn/fail usually means |
| --- | --- | --- |
| `config` | `clauster.yml` found and valid | `no config found: ...` / `config is not valid YAML: ...` / `invalid config: ...` |
| `claude` | binary found, version ≥ `min_version` | not on PATH, or too old — see above |
| `claude-login` | usable `claude` credentials | not logged in — bridges will spawn then hang |
| `projects_root` | the directory exists | wrong path in config |
| `git` | `git` on PATH | create/clone features unavailable |
| `state_dir` | writable | permissions — see above |
| `auth` | configuration consistent | one of the two auth refusals above |
| `port` | port free | `already in use (Clauster already running?)` |
| `version` | build up to date | upstream has a newer release / stale checkout |
| `systemd` | `KillMode=process` (bridges survive a restart) | a restart would kill live pty bridges |
| `node-toolchain` | node resolvable for npx MCP servers | nvm-only node invisible to bridges |
| `workspace-trust` | `N/M discovered projects trusted` (OK when all are) | any untrusted discovered project — a session there will stall on the trust prompt (see above) |

The rest belong to two **prefixed families** that grow as optional pieces are
added, so read the prefix rather than expecting this page to enumerate them:

- **`extra:<name>`** — an optional Python extra. Today: `extra:pyte` (live
  terminal view), `extra:apprise` (outbound notifications), and on Windows
  `extra:pywinpty` (Interactive Sessions). A warn carries the exact install
  hint (e.g. `pip install 'clauster[notify]'`). `apprise` is shown **only when
  notifications will actually send** — `notifications.enabled` with at least one
  `notifications.urls` entry (#1016); with the channel off or no URL configured,
  runtime never imports apprise, so it isn't a nag worth carrying here. `pyte` and
  `pywinpty` are always shown when missing: pyte also reassembles the bridge
  connect-URL and pywinpty is the Windows Interactive-Session backend, so they
  matter beyond the opt-in live-terminal view.
- **`binary:<name>`** — a bundled companion binary managed by
  `clauster deps`. Today: `binary:claustrum` (Direct Session daemon, shown
  when `claustrum.enabled`) and on Windows `binary:shawl` (service wrapper).
  A warn shows the `clauster deps install <name>` remedy. Presence is resolved
  the same way the daemon spawns, so a configured
  [`claustrum.binary`](reference/config.md#claustrum-hosted-live-view-channel-claustrumconfig)
  path (the minimal-PATH workaround) counts as present rather than warning falsely.
  For claustrum the row also reports the **detected version** from `claustrum
  --version` and warns (advisorily — never a doctor failure) when it can't be
  confirmed at or above the release clauster pins. A `go install`-built binary
  keeps the unstamped `claustrum-dev` sentinel, so when `--version` can't confirm
  the floor doctor reads the module version Go embeds in every binary and names
  the release outright — `claustrum v1.3.1 < required v1.7.1` rather than a
  can't-tell shrug. That fallback stays quiet unless it is certain: a build info
  blob that is absent, unreadable, records `(devel)`, or belongs to some other Go
  program leaves the original advisory in place. A separate `binary:<name>:shadow` warn
  appears when a managed `deps/bin` install exists but a different `PATH`/configured
  binary wins resolution, so you know which copy actually runs.

Family rows appear only when applicable on your platform and config, and they
warn, never fail: a missing optional piece leaves a feature dormant without
flipping doctor's exit code. `clauster deps list` shows the same inventory
with install state.

## Still stuck — filing a useful issue

### What to attach

- `clauster --version` and your OS / install method (pip, binary, Docker...).
- Full `clauster doctor` output — it is designed to be safe to share.
- The relevant server-log excerpt (`journalctl -u clauster` / `docker logs`).
  If `info` doesn't show the cause, reproduce with
  [`log_level: debug`](#raising-the-log-level) and attach that excerpt instead —
  scrubbed, as below.
- For bridge problems: the tail of the bridge's
  `<label>-<timestamp>-<seq>.log` — **scrubbed first, see below**.

The issue tracker is the support channel — see [Support](support.md).

### Scrubbing logs before you post them

!!! warning "The on-disk bridge log is not redacted by default"
    The redaction you see in the dashboard applies to the live stream only —
    with the default `logs.redact_session_url: false`, the on-disk log under
    `<state_dir>/logs/` is verbatim, including session URLs that act as
    bearer-equivalent credentials. Set `logs.redact_session_url: true` and
    reproduce if possible, then **review the log and scrub any remaining
    session URLs or secrets before sharing it** — even with redaction
    enabled, the redactor only recognizes known token shapes.

**Mechanism:** [Log redaction](security.md#log-redaction).
