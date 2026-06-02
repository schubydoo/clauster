# Clauster

A self-hosted web UI for spawning and managing Claude Code `remote-control`
bridges into arbitrary project directories on a remote host (NAS, homelab box),
from any browser or the Claude mobile app.

Anthropic's first-party tools assume terminal access on the host to spawn a
bridge in a new directory. Clauster fills that gap: a browser-based dispatcher
of `claude remote-control` instances on a remote machine. Once a bridge is
spawned, you attach to it via `claude.ai/code` or the mobile app.

> **Status: pre-1.0, in active development.** Loopback-only by default; password
> and reverse-proxy auth are available for networked deployments (see below).
> No telemetry, ever.

## Features

Everything below is implemented and shipping. Items marked **(opt-in)** are
gated behind a config flag and off by default — the flag is named inline so you
can find it in `clauster.yml.example`.

- **Project discovery** — one card per directory under `projects_root`, with
  git / `CLAUDE.md` / trust badges.
- **Bridge lifecycle** — start / stop / **restart** bridges; live status
  (Starting / Running / Stopped / Crashed). Restart re-spawns the bridge and
  reconnects its environment (it does *not* by itself restore the prior
  conversation — see the recap hook below). A bridge that launches but never
  registers an environment is reported honestly as `ERROR` after a grace
  window rather than a phantom `Running`.
- **Workspace trust** — a "Trust directory" action writes the Claude
  workspace-trust flag before spawning; untrusted directories are refused.
- **Spawn controls** — pick the spawn mode (same-dir / worktree / session) and
  permission mode per launch. `bypassPermissions` is double-gated: a per-project
  config ceiling (`projects.<name>.allow_bypass_permissions`) *and* a
  type-the-project-name confirm in the UI.
- **Open in Claude** — primary session deep link + a scannable QR code for
  attaching from your phone.
- **External session surfacing** — sessions you started from a terminal or
  Desktop (not via Clauster) are discovered and shown with a distinct indicator.
- **Live log tail** — the bridge debug log streamed over a WebSocket,
  ANSI-stripped and ID-redacted (`env_`/`session_`/`cse_` IDs, bare UUIDs, and
  secret-shaped tokens — API keys, bearer headers). Redaction is hybrid by
  default (verbatim on disk, redacted over the
  wire); `logs.redact_session_url` redacts on disk too.
- **CLAUDE.md editor** — view/edit a project's `CLAUDE.md` from the dashboard
  (size-capped, lost-update-guarded, trust-gated, audit-logged).
- **Create / clone projects** — make a new project or clone a git URL, with SSRF
  guards, transport lockdown, a size cap, and a "code runs on start" warning for
  cloned repos. Clones stream **live progress** over a WebSocket and never
  auto-spawn (they land discovered-but-stopped).
- **Per-project cost badge** — approximate USD + token totals rolled up from a
  project's session transcripts.
- **Ghost-environment reaper** — find and archive/delete the server-side bridge
  environments that outlive their bridge and clutter the claude.ai/code "New
  session" selector. The CLI (`clauster reap-environments`) is always available;
  the **dashboard UI is opt-in** (`reaper.ui_enabled`) because it exposes a
  destructive first-party API in the browser. Archive is reversible; force-delete
  requires typing `DELETE`.
- **Conversation recap on restart (opt-in)** — `claude remote-control` restarts
  into a fresh, empty context (it has no resume flag), so a restarted bridge
  "forgets" the prior conversation. With `claude.resume_recap` enabled, Clauster
  installs a `SessionStart` hook in the runtime user's Claude settings that
  recaps the most recent prior transcript for that directory back into the new
  session. Off by default — it edits the user's Claude settings and injects prior
  turns into context.
- **Auto-enable remote control** — before the first spawn, Clauster marks remote
  control as acknowledged in the runtime user's `~/.claude.json` so a
  detached-stdin bridge isn't stuck on the one-time interactive "Enable Remote
  Control?" prompt. On by default (`claude.auto_enable_remote_control`); set
  false to manage those flags yourself.

## Auth & networking

Loopback (`127.0.0.1`) needs no auth. Binding to a non-loopback address is
refused unless you enable one of: password login (`auth.password_required` +
a hash from `clauster hash-password`), reverse-proxy trust (peer-IP allowlist +
HMAC header), or an explicit `auth.allow_unauthenticated_network` opt-out for a
trusted LAN. Sessions are signed cookies with server-side revocation
("log out everywhere"); WebSocket connections are authenticated before accept
and origin-checked.

## Roadmap

Planned work, roughly in priority order. Nothing here is implemented yet; this
section is the public-facing companion to the in-repo `scratch/TODO.md`.

- **Fully reactive UI** — no full-page reloads anywhere (reactive project-card
  insertion), consistent pending/loading feedback on every action, and visible
  error surfacing throughout. Self-hosted icon set.
- **Native true-resume ("PTY mode")** — an optional bridge mode that spawns
  `claude --remote-control --continue` under a PTY for genuine conversation
  resume (vs. the recap hook's best-effort replay). Single-session, with
  different stop semantics; would sit alongside the current multi-session mode.
- **Public API** — promote the existing `/api/*` routes to a documented,
  versioned, auth-gated contract (OpenAPI surface, API tokens distinct from the
  session cookie) so third parties can build their own dashboards.
- **Session naming** — predictable/branded session display names instead of the
  random adjective-noun defaults; list active/resumable sessions in the UI.
- **v0.3 — multi-user** — per-user accounts (OIDC via Authentik / Pocket-ID /
  Keycloak / Zitadel), a real persistence layer (SQLAlchemy + Alembic), and GDPR
  controller tooling (`clauster user export` / `delete`).
- **v0.3 — operability** — crash notifications (Apprise / webhooks), a
  `/metrics` Prometheus endpoint, a homepage-dashboard widget endpoint, and i18n
  string extraction.
- **Wiki** — a proper docs/Wiki site (setup, deployment recipes, config
  reference, security model) beyond this README.

## Quick start (dev)

```sh
uv sync --extra dev
cp clauster.yml.example clauster.yml    # edit projects_root
uv run clauster
```

Then open <http://127.0.0.1:7621>.

## Docker

Multi-arch images (`linux/amd64`, `linux/arm64`) are published to GHCR on each release:

```sh
docker run -d --name clauster \
  -p 7621:7621 \
  -e PUID=1000 -e PGID=1000 \
  -e CLAUSTER_AUTH_PASSWORD_HASH="$(clauster hash-password)" \
  -v /path/to/config:/config \
  -v /path/to/projects:/projects \
  ghcr.io/schubydoo/clauster:latest
```

- The image binds `0.0.0.0`, which **requires auth** — set `CLAUSTER_AUTH_PASSWORD_HASH` (or put it in `/config/clauster.yml`) or the container exits on start.
- `/config` holds `clauster.yml` + state; `/projects` is your `projects_root`. `PUID`/`PGID` remap the runtime user to own bind-mounts.
- `claude` is **not** baked in (clauster spawns `claude remote-control`): mount it onto the container `PATH` along with `~/.claude` credentials, or build a derived image that installs it.
- Logs are JSON by default (`CLAUSTER_LOG_FORMAT`); health is at `/healthz`. Images are cosign-signed with build provenance + SBOM attestations.

## Configuration

All settings live in `clauster.yml` (see `clauster.yml.example` for the full,
commented schema). Any scalar key is overridable by an environment variable of
the form `CLAUSTER_<UPPER_SNAKE_PATH>` (e.g. `CLAUSTER_AUTH_PASSWORD_HASH`,
`CLAUSTER_REAPER_UI_ENABLED`). The schema is additive-only — old configs always
validate against newer versions.

## CLI

```
clauster run                  # start the server (default)
clauster hash-password        # generate an argon2id hash for auth
clauster doctor               # diagnose config / environment
clauster backup | restore | migrate
clauster install-service {systemd|launchd|windows}
clauster reap-environments    # reap ghost bridge environments (dry-run by default)
clauster usage <transcript>   # token + approximate cost for a session transcript
```

## Stack

Python 3.11+ · FastAPI · Alpine.js + Jinja2 · `uv` · `pydantic`. Developed and
CI-gated on Linux; macOS / Windows are in the test matrix. Apache-2.0 licensed.

## License

[Apache License 2.0](LICENSE).
