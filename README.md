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

- **Project discovery** — one card per directory under `projects_root`, with
  git / `CLAUDE.md` / trust badges.
- **Bridge lifecycle** — start / stop bridges; live status (Starting / Running /
  Stopped / Crashed); a "Trust directory" action that writes the workspace-trust
  flag before spawning.
- **Spawn controls** — pick the spawn mode (same-dir / worktree / session) and
  permission mode per launch. `bypassPermissions` is double-gated (a config
  ceiling + a typed-confirm).
- **Open in Claude** — primary session deep link + a scannable QR code.
- **Live log tail** — the bridge debug log streamed over a WebSocket,
  ANSI-stripped and ID-redacted.
- **CLAUDE.md editor** — view/edit a project's `CLAUDE.md` from the dashboard
  (size-capped, lost-update-guarded, audit-logged).
- **Create / clone projects** — make a new project or clone a git URL, with
  SSRF guards and a "code runs on start" warning for cloned repos.
- **Per-project cost badge** — approximate USD + token totals rolled up from a
  project's session transcripts.
- **Ghost-environment reaper** — find and archive/delete the server-side bridge
  environments that outlive their bridge (CLI always-on; opt-in dashboard UI).

## Auth & networking

Loopback (`127.0.0.1`) needs no auth. Binding to a non-loopback address is
refused unless you enable one of: password login (`auth.password_required` +
a hash from `clauster hash-password`), reverse-proxy trust (peer-IP allowlist +
HMAC header), or an explicit `auth.allow_unauthenticated_network` opt-out for a
trusted LAN. Sessions are signed cookies with server-side revocation
("log out everywhere"); WebSocket connections are authenticated before accept
and origin-checked.

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
CI-gated on Linux; macOS / Windows support is a target and in progress.
Apache-2.0 licensed.

## License

[Apache License 2.0](LICENSE).
