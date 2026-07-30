<h1 align="center">Clauster</h1>

<p align="center">
  <b>Run Claude Code on your own server — and drive it from your phone.</b><br>
  Clauster is a self-hosted web dashboard that starts Claude Code sessions in any
  project directory on your homelab, NAS, or VPS. You attach from
  <code>claude.ai/code</code> or the Claude mobile app. No SSH session required.
</p>

<p align="center">
  <a href="https://github.com/schubydoo/clauster/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/schubydoo/clauster/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://pypi.org/project/clauster/"><img alt="PyPI" src="https://img.shields.io/pypi/v/clauster?logo=pypi&logoColor=white"></a>
  <a href="https://github.com/schubydoo/clauster/blob/main/LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <a href="https://schubydoo.github.io/clauster/"><img alt="Docs" src="https://img.shields.io/badge/docs-schubydoo.github.io-informational"></a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/schubydoo/clauster/main/docs/screenshots/clauster-demo.gif" alt="Spawn a Claude session from the Clauster dashboard — open a project's launch menu, trust the directory, and the session starts, then shows Running under Active sessions" width="860">
</p>

## Why not just SSH in and run `claude`?

| | SSH + terminal | Clauster |
| --- | --- | --- |
| **From a phone** | Type into a mobile SSH client | Tap a project in a browser, then use the Claude app |
| **When you close the laptop** | Session dies with the shell | Session keeps running on the host; reattach later |
| **Starting a session** | `cd` to the project, remember the flags | One click per project, spawn + permission modes preset |

If you already live in a terminal on the same machine as your code, you don't need this.
Clauster is for when your code lives on **a box you don't want to SSH into every time**.

## Quick start

```sh
curl -fsSL https://raw.githubusercontent.com/schubydoo/clauster/main/install.sh | bash
clauster run
```

Open <http://127.0.0.1:7621> — run with no config and Clauster serves a
loopback-only **first-run setup wizard** that asks for your projects folder and a
dashboard password, writes a `clauster.yml`, and starts on it. Then click
**Run Claude here** on a project. Full recipes (uv / pip, Docker, systemd, reverse proxy) →
[Installation guide](https://schubydoo.github.io/clauster/latest/installation/).

> **Status: 1.0 — stable and actively developed.** Requires an Anthropic account
> with Claude Code access. Loopback-only by default; password and
> reverse-proxy auth are available for networked deployments (see
> [Auth & networking](#auth--networking)). **No telemetry, ever** — see
> [Privacy & data at rest](https://schubydoo.github.io/clauster/latest/privacy/) for what Clauster keeps locally.
> **Upgrading from 0.12?** 1.0 has five breaking changes — see
> [UPGRADING.md](https://github.com/schubydoo/clauster/blob/main/UPGRADING.md).

<table>
  <tr>
    <td width="50%" align="center">
      <img src="https://raw.githubusercontent.com/schubydoo/clauster/main/docs/screenshots/dashboard-light.png" alt="Dashboard in the light theme: an Active sessions zone listing two running sessions, above a list of four projects"><br>
      <sub><b>Light theme</b> — running sessions on top, projects below</sub>
    </td>
    <td width="50%" align="center">
      <img src="https://raw.githubusercontent.com/schubydoo/clauster/main/docs/screenshots/dashboard-dark.png" alt="The same dashboard in the dark theme"><br>
      <sub><b>Dark theme</b> — the toggle persists across reloads</sub>
    </td>
  </tr>
  <tr>
    <td width="50%" align="center">
      <img src="https://raw.githubusercontent.com/schubydoo/clauster/main/docs/screenshots/launch-menu.png" alt="A project's launch menu, offering three ways to drive a session — in claude.ai or Desktop, in the background, or in the browser — plus a permission-mode picker"><br>
      <sub><b>Launch menu</b> — pick how to drive it, and the permission mode</sub>
    </td>
    <td width="50%" align="center">
      <img src="https://raw.githubusercontent.com/schubydoo/clauster/main/docs/screenshots/new-project-clone.png" alt="The new-project panel with the clone-from-git-URL option selected"><br>
      <sub><b>Create or clone</b> — https/ssh only; cloning fetches files, runs nothing</sub>
    </td>
  </tr>
  <tr>
    <td width="50%" align="center">
      <img src="https://raw.githubusercontent.com/schubydoo/clauster/main/docs/screenshots/login-dark.png" alt="The Clauster password sign-in page"><br>
      <sub><b>Password login</b> — for non-loopback / networked deploys</sub>
    </td>
    <td width="50%" align="center" valign="middle">
      <sub>Every action is reactive — rows insert, badges flip, and clone progress<br>
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
[Quickstart](https://schubydoo.github.io/clauster/latest/quickstart/) or
[How it works](https://schubydoo.github.io/clauster/latest/concepts/how-it-works/).

<!-- Convention (#995): keep this list SHORT and version-agnostic. A README that
     makes no version-specific claims cannot drift; the ~1,148-word feature catalogue
     this replaced was a second copy of the docs site and went stale whenever `main`
     moved ahead of the newest release. Per-feature detail belongs on the docs site,
     which lives next to the code and is updated in the same PR. The site is versioned
     via `mike` (#1084) and defaults to the newest release, so links from here land on
     docs that match what a reader can install; unreleased docs are behind the `dev`
     entry in the version selector. The release notes for a tag remain the authority
     on what is actually in a given install. -->

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
[Installation guide](https://schubydoo.github.io/clauster/latest/installation/). To hack
on Clauster itself, see [Contributing](#contributing) below.

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
  state/config gone too. See [Privacy & data at rest](https://schubydoo.github.io/clauster/latest/privacy/#how-to-purge)
  and the [Installation guide](https://schubydoo.github.io/clauster/latest/installation/).

## Contributing

Working on Clauster itself? [`CONTRIBUTING.md`](https://github.com/schubydoo/clauster/blob/main/CONTRIBUTING.md) has the
from-source dev setup (`uv sync --extra dev`), the local test/lint gates, and
the branching + review workflow.

## First bridge in 60 seconds

Start Clauster (run `clauster`; it serves <http://127.0.0.1:7621> by default). With
an **authenticated `claude` on your `PATH`**, spawning your first bridge is a handful
of clicks — no terminal needed once it's started. Clauster *spawns* `claude` — it doesn't vendor it — and a spawned bridge
inherits the host user's `claude` authentication, so `claude` must be logged in
(interactive `claude` login **or** `ANTHROPIC_API_KEY` in the environment — either
satisfies the check) before any bridge can connect. `clauster doctor` (step 2)
confirms it; see the [Quickstart prerequisites](https://schubydoo.github.io/clauster/latest/quickstart/) for the full
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
   [configuration guide](https://schubydoo.github.io/clauster/latest/guides/configuration/)).
   For a deliberate fresh start,
   **Forget** the stopped session and launch again with **Run Claude here**.

> Exposing this beyond loopback (e.g. on your LAN)? Read
> [Auth & networking](#auth--networking) first — a non-loopback bind requires
> authentication.

## Docker

Multi-arch images (`linux/amd64`, `linux/arm64`) are published to GHCR on each
release, cosign-signed with provenance + SBOM attestations. Two things to know
before `docker run`:

- The image binds `0.0.0.0`, so it **refuses to start without enforced auth** —
  set `CLAUSTER_AUTH_ENABLED=true`, `CLAUSTER_AUTH_PASSWORD_REQUIRED=true`, and a
  `CLAUSTER_AUTH_PASSWORD_HASH` (generate one with
  `docker run --rm -it ghcr.io/schubydoo/clauster:latest clauster hash-password`).
- `claude` is **not baked into the image** — mount the CLI onto the container
  `PATH` (or set `CLAUSTER_CLAUDE_BINARY`) along with the runtime user's
  `~/.claude` credentials.

Full run + [`compose.yaml`](https://github.com/schubydoo/clauster/blob/main/compose.yaml) recipes, volumes, and PUID/PGID
mapping: [Installation → Docker](https://schubydoo.github.io/clauster/latest/installation/#docker).

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
[`docs/reference/config.md`](https://schubydoo.github.io/clauster/latest/reference/config/) is the exhaustive per-key reference. Any
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
[docs/reference/cli.md](https://schubydoo.github.io/clauster/latest/reference/cli/).

## Roadmap

Planned work, roughly in priority order — the public-facing companion to the in-repo
`scratch/TODO.md`.

- **Friendlier default session names** — Server-Mode bridges already accept a custom launch
  name (#780) and the dashboard lists active/resumable sessions per project; the remaining
  work is a more predictable *default* than the random adjective-noun one, and extending
  custom names to Interactive Sessions.

Not planned: clauster is a **single-operator** tool — multi-user accounts, OIDC login,
and per-user GDPR tooling were considered and declined (one deployment serves one
operator; isolate with separate instances instead). The UI is English-only and not
localized — there is no i18n string extraction on the roadmap (re-scope only if a real
translation contributor appears).

*Shipped:*

- **Public API** — a documented, versioned [`/api/v1`](https://schubydoo.github.io/clauster/latest/public-api/) surface with
  named Bearer tokens (distinct from the session cookie), managed via
  `clauster api-token issue|list|rotate|revoke`; an opt-in OpenAPI schema
  (`api.openapi_enabled`) is available for third-party dashboards.
- The in-repo [`docs/`](https://schubydoo.github.io/clauster/) pages (setup, networking, config reference,
  security model) are published as a live docs site at
  [schubydoo.github.io/clauster](https://schubydoo.github.io/clauster/).

## Stack

Python 3.11+ · FastAPI · Alpine.js + Jinja2 + Tabler · `uv` · `pydantic`. Developed
and CI-gated on Linux; macOS / Windows are in the test matrix. Apache-2.0 licensed.

## Project health

<p>
  <a href="https://github.com/schubydoo/clauster/actions/workflows/lint.yml"><img alt="Lint" src="https://github.com/schubydoo/clauster/actions/workflows/lint.yml/badge.svg"></a>
  <a href="https://codecov.io/gh/schubydoo/clauster"><img alt="codecov" src="https://codecov.io/gh/schubydoo/clauster/graph/badge.svg"></a>
  <a href="https://greptile.com"><img alt="Reviewed by Greptile" src="https://img.shields.io/badge/Greptile-reviewed-7C3AED"></a>
  <a href="https://scorecard.dev/viewer/?uri=github.com/schubydoo/clauster"><img alt="OpenSSF Scorecard" src="https://api.securityscorecards.dev/projects/github.com/schubydoo/clauster/badge"></a>
  <a href="https://www.bestpractices.dev/projects/13081"><img alt="OpenSSF Best Practices" src="https://www.bestpractices.dev/projects/13081/badge"></a>
  <a href="https://pypi.org/project/clauster/"><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/clauster"></a>
  <a href="https://github.com/schubydoo/clauster/pkgs/container/clauster"><img alt="GHCR" src="https://img.shields.io/badge/ghcr.io-clauster-2496ED?logo=docker&logoColor=white"></a>
  <a href="https://github.com/astral-sh/ruff"><img alt="Ruff" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json"></a>
  <a href="https://pre-commit.com/"><img alt="pre-commit" src="https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white"></a>
</p>

## Support

Questions, bugs, and feature requests all go through
[GitHub Issues](https://github.com/schubydoo/clauster/issues) — Discussions are
intentionally not enabled. See [`SUPPORT.md`](https://github.com/schubydoo/clauster/blob/main/SUPPORT.md) for how to get help, and
[`SECURITY.md`](https://github.com/schubydoo/clauster/blob/main/SECURITY.md) to report a vulnerability privately.

## License

[Apache License 2.0](https://github.com/schubydoo/clauster/blob/main/LICENSE).
