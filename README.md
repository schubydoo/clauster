<h1 align="center">Clauster</h1>

<p align="center">
  <em>A self-hosted web dashboard for spawning and managing Claude Code <code>remote-control</code><br>
  bridges into any project directory on a remote host — then attach to them from<br>
  <code>claude.ai/code</code> or the Claude mobile app. No SSH session required.</em>
</p>

<p align="center">
  <a href="https://github.com/schubydoo/clauster/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/schubydoo/clauster/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/schubydoo/clauster/actions/workflows/lint.yml"><img alt="Lint" src="https://github.com/schubydoo/clauster/actions/workflows/lint.yml/badge.svg"></a>
  <a href="https://codecov.io/gh/schubydoo/clauster"><img alt="codecov" src="https://codecov.io/gh/schubydoo/clauster/graph/badge.svg"></a>
  <a href="https://greptile.com"><img alt="Reviewed by Greptile" src="https://img.shields.io/badge/Greptile-reviewed-7C3AED"></a>
  <a href="https://scorecard.dev/viewer/?uri=github.com/schubydoo/clauster"><img alt="OpenSSF Scorecard" src="https://api.securityscorecards.dev/projects/github.com/schubydoo/clauster/badge"></a>
  <a href="https://www.bestpractices.dev/projects/13081"><img alt="OpenSSF Best Practices" src="https://www.bestpractices.dev/projects/13081/badge"></a>
</p>

<p align="center">
  <a href="https://pypi.org/project/clauster/"><img alt="PyPI" src="https://img.shields.io/pypi/v/clauster?logo=pypi&logoColor=white"></a>
  <a href="https://pypi.org/project/clauster/"><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/clauster"></a>
  <a href="https://github.com/schubydoo/clauster/blob/main/LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <a href="https://github.com/schubydoo/clauster/pkgs/container/clauster"><img alt="GHCR" src="https://img.shields.io/badge/ghcr.io-clauster-2496ED?logo=docker&logoColor=white"></a>
  <a href="https://github.com/astral-sh/ruff"><img alt="Ruff" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json"></a>
  <a href="https://pre-commit.com/"><img alt="pre-commit" src="https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white"></a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/schubydoo/clauster/main/docs/screenshots/clauster-demo.gif" alt="Spawn a Claude session from the Clauster dashboard — open a project's launch menu, trust the directory, and the session starts, then shows Running under Active sessions" width="860">
</p>

Anthropic's first-party tooling assumes terminal access on the host to spawn a
bridge in a given project directory. Clauster fills that gap: a browser-based dispatcher of
`claude remote-control` instances on a remote machine (NAS, homelab box). You pick
a project, start a bridge, and attach to it from `claude.ai/code` or the mobile app.

> **Status: pre-1.0, in active development.** Loopback-only by default; password and
> reverse-proxy auth are available for networked deployments (see
> [Auth & networking](#auth--networking)). **No telemetry, ever** — see
> [Privacy & data at rest](https://schubydoo.github.io/clauster/privacy/) for what Clauster keeps locally.

**Install now:** `curl -fsSL https://raw.githubusercontent.com/schubydoo/clauster/main/install.sh | bash`

The full install recipes and verification steps are in the
[Installation guide](https://schubydoo.github.io/clauster/installation/).

<table>
  <tr>
    <td width="50%" align="center">
      <img src="https://raw.githubusercontent.com/schubydoo/clauster/main/docs/screenshots/dashboard-light.png" alt="Dashboard, light theme"><br>
      <sub><b>Dark / light</b> — theme toggle persists across reloads</sub>
    </td>
    <td width="50%" align="center">
      <img src="https://raw.githubusercontent.com/schubydoo/clauster/main/docs/screenshots/new-project-clone.png" alt="Create or clone a project"><br>
      <sub><b>Create or clone</b> — SSRF-guarded, cloned code runs only on Start</sub>
    </td>
  </tr>
  <tr>
    <td width="50%" align="center">
      <img src="https://raw.githubusercontent.com/schubydoo/clauster/main/docs/screenshots/login-dark.png" alt="Password login"><br>
      <sub><b>Password login</b> — for non-loopback / networked deploys</sub>
    </td>
    <td width="50%" align="center" valign="middle">
      <sub>Every action is reactive — cards insert, badges flip, and clone progress<br>
      streams without a full-page reload. Self-hosted assets; no CDN, no trackers.</sub>
    </td>
  </tr>
</table>

## Features

A self-hosted dashboard for the `claude` sessions running on your own machine —
start them, watch them, and pick them up from your phone.

- **Projects & bridges** — a card per directory under `projects_root`; start, stop
  and resume bridges, choose the spawn and permission mode, and open any session in
  Claude by deep link or QR code.
- **Visibility** — live bridge-log tail over a WebSocket, a read-only transcript
  viewer, and per-project cost and token totals.
- **In-app editing** — edit a project's Claude Code config surfaces and an allowlist
  of operational `clauster.yml` settings, without reaching for SSH.
- **Safety** — workspace-trust prompts before a first spawn, session URLs redacted
  out of the streamed log (the on-disk bridge log stays verbatim unless you set
  `logs.redact_session_url`), and an auth model that fails closed on a network bind.
- **Interactive Sessions** — an opt-in pty mode with true resume and a read-only live
  terminal view.
- **Extras** — outbound notifications and webhooks, a Prometheus endpoint, a
  ghost-environment reaper, **experimental** background agents, and an MCP server
  (read-only until you opt into its write tools). Each is off, or read-only, by
  default.

That is the short list, not the whole of it — every feature is documented in full on
the **[documentation site](https://schubydoo.github.io/clauster/)**; start at the
[Quickstart](https://schubydoo.github.io/clauster/quickstart/) or
[How it works](https://schubydoo.github.io/clauster/concepts/how-it-works/).

<!-- Convention (#995): keep this list SHORT and version-agnostic. A README that
     makes no version-specific claims cannot drift; the ~1,148-word feature catalogue
     this replaced was a second copy of the docs site and went stale whenever `main`
     moved ahead of the newest release. Per-feature detail belongs on the docs site,
     which lives next to the code and is updated in the same PR. Note the site is
     built from `main` on every push and is NOT versioned (no `mike`), so it too can
     describe unreleased behaviour — the release notes for the newest tag are the
     authority on what is actually in a given install. -->

## Install

No Python needed — the install script grabs the signed standalone binary for your
OS, verifies its checksum, and installs it to `~/.local/bin` (Linux & macOS),
printing a PATH hint if that directory isn't already on your `PATH`:

```sh
curl -fsSL https://raw.githubusercontent.com/schubydoo/clauster/main/install.sh | bash
```

On **Windows**, the PowerShell equivalent installs `clauster.exe` the same way:

```powershell
irm https://raw.githubusercontent.com/schubydoo/clauster/main/install.ps1 | iex
```

Or pick another path — `uv tool install clauster` (recommended for a Python host),
`pip`/`pipx`, [Scoop](https://scoop.sh) on Windows (`scoop bucket add clauster
https://github.com/schubydoo/clauster && scoop install clauster`), or
[Docker](#docker). Full recipes — including supply-chain verification — are in the
[Installation guide](https://schubydoo.github.io/clauster/installation/). To hack
on Clauster itself, see [CONTRIBUTING.md](https://github.com/schubydoo/clauster/blob/main/CONTRIBUTING.md) for the from-source path.

### Uninstall

- Install script / standalone binary: remove `~/.local/bin/clauster` on Linux or
  macOS; on Windows, delete `%LOCALAPPDATA%\Programs\clauster\clauster.exe`
  unless you set a custom install directory.
- Python tools: run `uv tool uninstall clauster`, `pipx uninstall clauster`, or
  `pip uninstall clauster`, matching how you installed it.
- Scoop: run `scoop uninstall clauster`.
- Docker: stop and remove the container with `docker rm -f clauster`, then remove
  the image with `docker rmi ghcr.io/schubydoo/clauster:latest` (substitute the
  pinned tag you pulled, e.g. `:X.Y.Z`, if you used a specific release) if you no
  longer need it. Docker Compose users: run `docker compose down` from the directory
  containing `compose.yaml` (add `-v` to also delete named volumes).
- Full purge: stop Clauster first, then remove your `state_dir` if you want local
  state/config gone too. See [Privacy & data at rest](https://schubydoo.github.io/clauster/privacy/#how-to-purge)
  and the [Installation guide](https://schubydoo.github.io/clauster/installation/).

## First bridge in 60 seconds

Start Clauster (run `clauster`; it serves <http://127.0.0.1:7621> by default). With
an **authenticated `claude` on your `PATH`**, spawning your first bridge is a handful
of clicks — no terminal needed once it's started. Clauster *spawns* `claude` — it doesn't vendor it — and a spawned bridge
inherits the host user's `claude` authentication, so `claude` must be logged in
(interactive `claude` login **or** `ANTHROPIC_API_KEY` in the environment — either
satisfies the check) before any bridge can connect. `clauster doctor` (step 2)
confirms it; see the [Quickstart prerequisites](https://schubydoo.github.io/clauster/quickstart/) for the full
list.

1. **Point Clauster at your code.** Set `projects_root` in `clauster.yml` to a
   directory whose subfolders are projects (e.g. `~/code`); each child directory
   becomes a card.
2. **Sanity-check the host (optional).** `clauster doctor` confirms `claude` is found,
   new enough, and **logged in** (the bridge inherits this login), and that
   `projects_root` / the state dir are usable — fix any ✗ before spawning.
3. **Open the dashboard** at <http://127.0.0.1:7621>. You'll see one card per project
   to start — a project running several sessions later shows a card for each.
4. **Launch a bridge.** On a project's card, click **Run Claude here ▾**, choose
   **In claude.ai / Desktop**, pick a permission mode, then **Run**. Clauster
   launches `claude remote-control` in that directory and the card flips to
   *Running* with a live status badge. (The spawn-mode and permission defaults are
   safe out of the box.)
5. **Attach from anywhere.** Use the card's **Open in Claude** link — or scan its
   **QR code** — to pick the bridge up in `claude.ai/code` or the Claude mobile app.
   No SSH session.
6. **Stop or resume.** **Stop** signals the bridge; **Resume** relaunches it (with
   `claude.resume_recap` or `claude.launch_mode: pty` it can carry the prior conversation
   forward — see the
   [configuration guide](https://schubydoo.github.io/clauster/guides/configuration/)).
   For a deliberate fresh start,
   **Forget** the stopped session and launch again with **Run Claude here**.

> Exposing this beyond loopback (e.g. on your LAN)? Read
> [Auth & networking](#auth--networking) first — a non-loopback bind requires
> authentication.

## Docker

Multi-arch images (`linux/amd64`, `linux/arm64`) are published to GHCR on each release.
The image binds `0.0.0.0`, so it **requires enforced auth** to start. First generate a
password hash — this runs `clauster` *inside* the image, so you don't need it on the host:

```sh
docker run --rm -it ghcr.io/schubydoo/clauster:latest clauster hash-password
```

Copy the printed `$argon2id$…` hash, then start the server with auth enabled:

```sh
docker run -d --name clauster \
  -p 7621:7621 \
  -e PUID=1000 -e PGID=1000 \
  -e CLAUSTER_AUTH_ENABLED=true \
  -e CLAUSTER_AUTH_PASSWORD_REQUIRED=true \
  -e 'CLAUSTER_AUTH_PASSWORD_HASH=$argon2id$v=19$...' \
  -v /path/to/config:/config \
  -v /path/to/projects:/projects \
  ghcr.io/schubydoo/clauster:latest
```

- The image binds `0.0.0.0`, so it won't start without **enforced** auth — set
  `CLAUSTER_AUTH_ENABLED=true` **and** `CLAUSTER_AUTH_PASSWORD_REQUIRED=true` **and** a
  `CLAUSTER_AUTH_PASSWORD_HASH` (or configure reverse-proxy trust in `/config/clauster.yml`),
  or the container exits on start. Single-quote the hash env value — the argon2 hash
  contains `$` that your shell would otherwise expand.
- `/config` holds `clauster.yml` + state; `/projects` is your `projects_root`.
  `PUID`/`PGID` remap the runtime user to own bind-mounts.
- `claude` is **not** baked in — tell Clauster where it is one of two ways: mount the
  binary somewhere on the container `PATH` (the default `claude.binary: claude` is
  resolved via `PATH`), **or** set `CLAUSTER_CLAUDE_BINARY=/abs/path/to/claude` (a.k.a.
  `claude.binary`) to an absolute path you've mounted anywhere. Either way, also mount
  the runtime user's `~/.claude` credentials — or build a derived image that installs
  `claude`.
- Logs are human text by default; set `CLAUSTER_LOG_FORMAT=json` for structured
  JSON (both redact session URLs / bearer ids). Health is at `/healthz`. Images
  are cosign-signed with build provenance + SBOM attestations.

### First-run setup wizard (Docker)

Prefer a browser to the `hash-password` + env-var recipe above? Start the container with an
**empty `/config`** and **without** the `CLAUSTER_AUTH_*` vars — Clauster serves the first-run
[setup wizard](https://schubydoo.github.io/clauster/guides/configuration/) instead. In a container the wizard binds all interfaces
(so a published port can reach it) and is gated by a **one-time token** printed to the log:

```sh
docker run -d --name clauster -p 7621:7621 \
  -e PUID=1000 -e PGID=1000 \
  -v /path/to/config:/config -v /path/to/projects:/projects \
  ghcr.io/schubydoo/clauster:latest
docker logs clauster        # → "...first-run setup at http://<this-host>:7621/?token=… (will write /config/clauster.yml)"
```

Open that URL (substituting the host that reaches the container) — the token gates access, so
copy it exactly. Set `projects_root` (`/projects`), a bind host, and a password; the wizard
writes an **auth-enabled** `/config/clauster.yml` on the persistent volume and restarts onto it.
Because the config lands on `/config` (not the container's ephemeral layer), it survives
`docker rm` / image updates. The token is single-use for that first boot; once a config exists
the wizard never runs again.

### Docker Compose

A ready-to-edit [`compose.yaml`](https://github.com/schubydoo/clauster/blob/main/compose.yaml) is included:

```sh
# 1. generate a password hash (runs inside the image)
docker compose run --rm clauster clauster hash-password
# 2. export it single-quoted, then edit the projects/claude volumes in compose.yaml
export CLAUSTER_AUTH_PASSWORD_HASH='$argon2id$v=19$...'
# 3. start (the image's HEALTHCHECK is inherited)
docker compose up -d
```

## Auth & networking

Loopback (`127.0.0.1`) needs no auth. Binding to a non-loopback address is refused
unless authentication is actually enforced — set `auth.enabled: true` (the master
switch) together with either password login (`auth.password_required` + a hash from
`clauster hash-password`) or reverse-proxy trust (peer-IP allowlist + HMAC header) —
or, to opt out on a trusted LAN, `auth.allow_unauthenticated_network`. Sessions
are signed cookies with server-side revocation ("log out everywhere"); WebSocket
connections are authenticated before accept and origin-checked. The origin
allowlist is a cross-site defence rather than an authentication method, so it is
enforced even with `auth.enabled: false`; a non-loopback bind auto-trusts no
origin and must set `auth.allowed_origins`.

## Configuration

All settings live in `clauster.yml` —
[`clauster.yml.example`](https://github.com/schubydoo/clauster/blob/main/clauster.yml.example) is a lean starter, and
[`docs/reference/config.md`](https://schubydoo.github.io/clauster/reference/config/) is the exhaustive per-key reference. Any
scalar or list key is overridable by an environment variable of the form
`CLAUSTER_<UPPER_SNAKE_PATH>` (lists take a comma-separated value). The schema is
additive-only — old configs always validate against newer versions.

| Common flag | Default | What it does |
| --- | --- | --- |
| `host` / `port` | `127.0.0.1` / `7621` | bind address (non-loopback needs auth) |
| `projects_root` | — | directory whose children become project cards |
| `auth.enabled` | `false` | master auth switch — must be on for password / proxy auth to apply |
| `auth.password_required` | `false` | require login (`clauster hash-password` for the hash) |
| `claude.resume_recap` | `false` | recap the prior transcript into a restarted bridge |
| `claude.launch_mode` | `standard` | `pty` = native true-resume on Resume (POSIX pty, or Windows ConPTY with the `pty` extra); default for new bridges only — a bridge keeps the mode it launched with |
| `reaper.ui_enabled` | `false` | expose the ghost-environment reaper in the dashboard |
| `claustrum.enabled` | `false` | enable the hosted live-view channel (connect-or-spawn the `claustrum` daemon) |
| `usage.mode` | `cost` | per-project badge contents: `cost` (≈USD + tokens) · `tokens` (count only) · `off` (hide + skip the usage fetch). `usage.show_cost: false` is a deprecated alias for `off` |
| `logs.redact_session_url` | `false` | redact the session URL on disk too, not just over WS |

## CLI

```text
clauster run                  # start the server (default)
clauster hash-password        # generate an argon2id hash for auth.password_hash
clauster hash-token           # mint an API token + hash for auth.api_token_hash
clauster hash-metrics-token   # mint a /metrics scrape token + hash for observability.metrics_token_hash
clauster api-token issue|list|rotate|revoke   # manage named public-API bearer tokens
clauster mcp                  # read-only MCP server over stdio (list + status)
clauster doctor               # diagnose config / environment
clauster backup | restore | migrate
clauster install-service {systemd|launchd|windows}
clauster reap-environments    # reap ghost bridge environments (dry-run by default)
clauster keepers              # list or stop orphaned pty keepers
clauster usage <transcript>   # token + approximate cost for a session transcript
clauster config reconcile     # remove deprecated config keys, writing their replacements
clauster deps list|install|uninstall          # manage optional extras beside the standalone binary
clauster projects | status | sessions         # headless reads (--json), no server needed
clauster logs <instance> | open <instance>    # tail a bridge's redacted log / print its connect URL
clauster start <project> | stop <instance>    # headless spawn/stop through the same engine as the UI
#   <instance> = full id, a unique prefix of one (as printed by `status`), or a project name
```

Full per-command reference:
[docs/reference/cli.md](https://schubydoo.github.io/clauster/reference/cli/).

## Roadmap

Planned work, roughly in priority order — the public-facing companion to the in-repo
`scratch/TODO.md`.

- **Session naming** — predictable/branded session display names instead of the
  random adjective-noun defaults; list active/resumable sessions in the UI.

Not planned: clauster is a **single-operator** tool — multi-user accounts, OIDC login,
and per-user GDPR tooling were considered and declined (one deployment serves one
operator; isolate with separate instances instead). The UI is English-only and not
localized — there is no i18n string extraction on the roadmap (re-scope only if a real
translation contributor appears).

*Shipped:*

- **Public API** — a documented, versioned [`/api/v1`](https://schubydoo.github.io/clauster/public-api/) surface with
  named Bearer tokens (distinct from the session cookie), managed via
  `clauster api-token issue|list|rotate|revoke`; an opt-in OpenAPI schema
  (`api.openapi_enabled`) is available for third-party dashboards.
- The in-repo [`docs/`](https://schubydoo.github.io/clauster/) pages (setup, networking, config reference,
  security model) are published as a live docs site at
  [schubydoo.github.io/clauster](https://schubydoo.github.io/clauster/).

## Stack

Python 3.11+ · FastAPI · Alpine.js + Jinja2 + Tabler · `uv` · `pydantic`. Developed
and CI-gated on Linux; macOS / Windows are in the test matrix. Apache-2.0 licensed.

## Support

Questions, bugs, and feature requests all go through
[GitHub Issues](https://github.com/schubydoo/clauster/issues) — Discussions are
intentionally not enabled. See [`SUPPORT.md`](https://github.com/schubydoo/clauster/blob/main/SUPPORT.md) for how to get help, and
[`SECURITY.md`](https://github.com/schubydoo/clauster/blob/main/SECURITY.md) to report a vulnerability privately.

## License

[Apache License 2.0](https://github.com/schubydoo/clauster/blob/main/LICENSE).
