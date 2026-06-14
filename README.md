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
  <a href="https://coderabbit.ai"><img alt="Reviewed by CodeRabbit" src="https://img.shields.io/badge/CodeRabbit-reviewed-FF570A?logo=coderabbit&logoColor=white"></a>
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
  <img src="https://raw.githubusercontent.com/schubydoo/clauster/main/docs/screenshots/dashboard-dark.png" alt="Clauster dashboard" width="860">
</p>

Anthropic's first-party tooling assumes terminal access on the host to spawn a
bridge in a given project directory. Clauster fills that gap: a browser-based dispatcher of
`claude remote-control` instances on a remote machine (NAS, homelab box). You pick
a project, start a bridge, and attach to it from `claude.ai/code` or the mobile app
— no SSH session required.

> **Status: pre-1.0, in active development.** Loopback-only by default; password and
> reverse-proxy auth are available for networked deployments (see
> [Auth & networking](#auth--networking)). **No telemetry, ever.**

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

Everything below is implemented and shipping. Items marked **(opt-in)** are gated
behind a config flag and off by default — the flag is named inline so you can find
it in [`clauster.yml.example`](https://github.com/schubydoo/clauster/blob/main/clauster.yml.example).

### Projects & bridges

- **Project discovery** — one card per directory under `projects_root`, with git /
  `CLAUDE.md` / trust badges.
- **Bridge lifecycle** — start / stop / **resume** bridges; live status
  (Starting / Running / Stopped / Crashed / Error). A bridge that launches but
  never registers an environment is reported honestly as `Error` after a grace
  window, not a phantom `Running`.
- **Spawn controls** — pick the spawn mode (same-dir / worktree / session),
  permission mode, and resume mode (standard / pty true-resume, POSIX) per launch;
  `claude.resume_mode` is the pre-selected default. `bypassPermissions` is
  double-gated: a per-project config ceiling
  (`projects.<name>.allow_bypass_permissions`) **and** a type-the-project-name
  confirm in the UI.
- **Open session in Claude** — a deep link to the primary session plus a scannable QR code
  that opens it in the Claude app (claude.ai/code or mobile), attached to the
  running bridge.
- **External session surfacing** — sessions you started from a terminal or Desktop
  (not via Clauster) are discovered and shown with a distinct indicator.
- **Create / clone projects** — make a new project or clone a git URL, with SSRF
  guards, transport lockdown, a size cap, and a "code runs on start" warning for
  cloned repos. Clones stream **live progress** over a WebSocket and never
  auto-spawn (they land discovered-but-stopped).

### Visibility & editing

- **Live log tail** — the bridge debug log streamed over a WebSocket, ANSI-stripped
  and ID-redacted (`env_`/`session_`/`cse_` IDs, bare UUIDs, and secret-shaped
  tokens — API keys, bearer headers). Redaction is hybrid by default (verbatim on
  disk, redacted over the wire); `logs.redact_session_url` redacts on disk too.
- **CLAUDE.md editor** — view/edit a project's `CLAUDE.md` from the dashboard
  (size-capped, lost-update-guarded, trust-gated, audit-logged).
- **Per-project cost badge** — approximate USD + token totals rolled up from a
  project's session transcripts. Token counts are exact (read from the transcript
  `usage`); the dollar figure is a ballpark — a hand-maintained USD price table
  (`usage.py`, as of 2026-05) that drifts as pricing changes, with unpriced models
  counting as 0. `usage.mode` selects what the badge shows — `cost`, `tokens`, or
  `off` (hide it for privacy / screen-share); `usage.show_cost: false` is a
  deprecated alias for `off`.

### Safety

- **Workspace trust** — starting a bridge in an untrusted directory prompts a
  just-in-time "trust the files in this folder?" confirm (an explicit checkbox) that
  writes the Claude workspace-trust flag and then spawns; trusted directories show a
  green shield by the project name and start with no prompt.
- **Auto-enable remote control** — before the first spawn, Clauster marks remote
  control as acknowledged in the runtime user's `~/.claude.json` so a detached-stdin
  bridge isn't stuck on the one-time interactive "Enable Remote Control?" prompt. On
  by default (`claude.auto_enable_remote_control`); set false to manage it yourself.

### Opt-in extras

- **Conversation recap on restart (opt-in)** — `claude remote-control` restarts into
  a fresh, empty context, so a restarted bridge "forgets" the prior conversation.
  With `claude.resume_recap` enabled, Clauster installs a `SessionStart` hook in the
  runtime user's Claude settings that recaps the most recent prior transcript for
  that directory back into the new session.
- **Native true-resume / "PTY mode" (opt-in, POSIX)** — `claude.resume_mode: pty`
  runs the `claude --remote-control` flag form under a PTY *keeper* sidecar, which
  **genuinely restores prior conversation context** on Resume (`--continue`) rather
  than recapping it. The keeper outlives a Clauster restart and is stopped by signal.
  Single-session (vs. the default multi-session server). The dashboard surfaces the
  resume mode per bridge and rediscovers a running pty bridge after a Clauster restart.
- **Ghost-environment reaper** — find and archive/delete the server-side bridge
  environments that outlive their bridge and clutter the claude.ai/code "New session"
  selector. The CLI (`clauster reap-environments`) is always available; the
  **dashboard UI is opt-in** (`reaper.ui_enabled`) because it exposes a destructive
  first-party API in the browser. Archive is reversible; force-delete requires
  typing `DELETE`.
- **Background agents (experimental)** — a dashboard panel that lists, dispatches,
  and stops Claude Code *background* sessions (`claude --bg`), backed by
  `GET/POST/DELETE /api/agents`. It rides Claude Code's **agent-view research
  preview**, so it's experimental and may change with the upstream CLI.
- **Hosted live-view channel (opt-in, experimental)** — an alternate substrate to
  the remote-control bridge. With `claustrum.enabled: true`, Clauster connect-or-spawns
  a single `claustrum` daemon per deployment and runs `claude` headless over its
  stream-json channel, streaming the session **live in the browser** (with permission
  prompts surfaced in the UI) instead of being driven from Claude Desktop / claude.ai.
  Off by default and fail-closed — an unreachable daemon surfaces in `/healthz` and
  never affects the bridge lifecycle. Requires the separate `claustrum` binary.

## Quick start (dev)

```sh
uv sync --extra dev
cp clauster.yml.example clauster.yml    # edit projects_root
uv run clauster
```

Then open <http://127.0.0.1:7621>. `claude` must be on your `PATH` (Clauster spawns
it; it isn't vendored).

## First bridge in 60 seconds

With the server running (above) and `claude` on your `PATH`, spawning your first
bridge is a handful of clicks — no terminal needed once it's started:

1. **Point Clauster at your code.** Set `projects_root` in `clauster.yml` to a
   directory whose subfolders are projects (e.g. `~/code`); each child directory
   becomes a card.
2. **Sanity-check the host (optional).** `clauster doctor` confirms `claude` is found
   and new enough and that `projects_root` / the state dir are usable — fix any ✗
   before spawning.
3. **Open the dashboard** at <http://127.0.0.1:7621>. You'll see one card per project.
4. **Start a bridge.** On a project's card, click **Start**. Clauster launches
   `claude remote-control` in that directory and the card flips to *Running* with a
   live status badge. (Pick a spawn / permission mode first if you like — the
   defaults are safe.)
5. **Attach from anywhere.** Use the card's **Open session in Claude** link — or scan its
   **QR code** — to pick the bridge up in `claude.ai/code` or the Claude mobile app.
   No SSH session.
6. **Stop or resume.** **Stop** signals the bridge; **Resume** relaunches it (with
   `claude.resume_recap` or `resume_mode: pty` it can carry the prior conversation
   forward — see [Opt-in extras](#opt-in-extras)). A resumable bridge also offers
   **Start new session** for a deliberate fresh start.

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
- Logs are JSON by default (`CLAUSTER_LOG_FORMAT`); health is at `/healthz`. Images
  are cosign-signed with build provenance + SBOM attestations.

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
connections are authenticated before accept and origin-checked.

## Configuration

All settings live in `clauster.yml` — see
[`clauster.yml.example`](https://github.com/schubydoo/clauster/blob/main/clauster.yml.example) for the full, commented schema. Any
scalar key is overridable by an environment variable of the form
`CLAUSTER_<UPPER_SNAKE_PATH>`. The schema is additive-only — old configs always
validate against newer versions.

| Common flag | Default | What it does |
| --- | --- | --- |
| `host` / `port` | `127.0.0.1` / `7621` | bind address (non-loopback needs auth) |
| `projects_root` | — | directory whose children become project cards |
| `auth.enabled` | `false` | master auth switch — must be on for password / proxy auth to apply |
| `auth.password_required` | `false` | require login (`clauster hash-password` for the hash) |
| `claude.resume_recap` | `false` | recap the prior transcript into a restarted bridge |
| `claude.resume_mode` | `standard` | `pty` = native true-resume on Resume (POSIX); default for new bridges only — a bridge keeps the mode it launched with |
| `reaper.ui_enabled` | `false` | expose the ghost-environment reaper in the dashboard |
| `claustrum.enabled` | `false` | enable the hosted live-view channel (connect-or-spawn the `claustrum` daemon) |
| `usage.mode` | `cost` | per-project badge contents: `cost` (≈USD + tokens) · `tokens` (count only) · `off` (hide + skip the usage fetch). `usage.show_cost: false` is a deprecated alias for `off` |
| `logs.redact_session_url` | `false` | redact the session URL on disk too, not just over WS |

## CLI

```text
clauster run                  # start the server (default)
clauster hash-password        # generate an argon2id hash for auth
clauster doctor               # diagnose config / environment
clauster backup | restore | migrate
clauster install-service {systemd|launchd|windows}
clauster reap-environments    # reap ghost bridge environments (dry-run by default)
clauster usage <transcript>   # token + approximate cost for a session transcript
```

## Roadmap

Planned work, roughly in priority order — the public-facing companion to the in-repo
`scratch/TODO.md`.

- **Public API** — promote the existing `/api/*` routes to a documented, versioned,
  auth-gated contract (OpenAPI surface, API tokens distinct from the session cookie)
  so third parties can build their own dashboards.
- **Session naming** — predictable/branded session display names instead of the
  random adjective-noun defaults; list active/resumable sessions in the UI.
- **v0.3 — multi-user** — per-user accounts (OIDC via Authentik / Pocket-ID /
  Keycloak / Zitadel), a real persistence layer (SQLAlchemy + Alembic), and GDPR
  controller tooling (`clauster user export` / `delete`).
- **v0.3 — operability** — a homepage-dashboard widget endpoint and i18n string
  extraction (crash notifications and the `/metrics` Prometheus endpoint already
  shipped).
- **Docs site** — publish the in-repo [`docs/`](docs/index.md) pages (setup,
  networking, config reference, security model) as a proper docs site.

## Stack

Python 3.11+ · FastAPI · Alpine.js + Jinja2 + Tabler · `uv` · `pydantic`. Developed
and CI-gated on Linux; macOS / Windows are in the test matrix. Apache-2.0 licensed.

## License

[Apache License 2.0](https://github.com/schubydoo/clauster/blob/main/LICENSE).
