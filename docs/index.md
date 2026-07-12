# Clauster

**Clauster** is a self-hosted FastAPI + Alpine/Tabler web app that spawns and
manages `claude` remote-control bridges on a remote host, driven entirely from
the browser. Point it at a directory of projects, click **Run Claude here** on a
project card and choose **In claude.ai / Desktop**, then pick the bridge up from
`claude.ai/code` or the Claude mobile app — no SSH session required.

It is published to PyPI as [`clauster`](https://pypi.org/project/clauster/),
shipped as a signed GHCR container image, and released as Sigstore-signed GitHub
Releases.

## The two bridge modes

A "bridge" is a `claude` process that Clauster launches in a project directory
and exposes to the Claude remote-control controller. Clauster supports two
launch modes, selected per instance via `claude.launch_mode`. They are **not
interchangeable** — each has its own argv and readiness logic.

| | **Server Mode** (default) | **Interactive Session** (opt-in) |
| --- | --- | --- |
| Command form | `claude remote-control` (subcommand) | `claude --remote-control` (flag) |
| Sessions | multi-session server | single session |
| Restart behaviour | spawns a fresh, empty context — no conversation resume | genuinely restores prior context (`--continue` true resume) |
| Process model | headless server | runs under a **PTY keeper** sidecar that owns the PTY |
| Platform | all | POSIX pty, or a Windows ConPTY keeper with the `pty` extra (else falls back to Server Mode) |

The mode is recorded on a bridge **at launch**: editing `claude.launch_mode`
seeds the mode for *new* bridges only and never re-modes a running or stopped
one (stop/resume always honour the recorded mode).

For users on **Server Mode** who still want continuity across a restart,
the opt-in `claude.resume_recap` SessionStart hook recaps the most recent prior
transcript for the directory back into a freshly restarted bridge. Unlike
Interactive Session mode, this *recaps* the prior conversation rather than truly restoring it.

## Key features

### Projects & bridges

- One card per child directory of your `projects_root`.
- **Run Claude here** (launch) / Stop / Resume a bridge from the card. For a
  deliberate fresh start, **Forget** a stopped session (drops it from Recent),
  then relaunch.
- Per-card **Open in Claude** link and **QR code** to attach from anywhere.
- Spawn modes (`same-dir`, `worktree`, `session`) and permission modes
  (`default`, `plan`, `acceptEdits`, `auto`, `dontAsk`, `bypassPermissions`).
- Create or clone projects into `projects_root` with progress streamed over a
  WebSocket.

### Visibility & editing

- Live bridge debug-log tail over a WebSocket — ANSI-stripped and ID-redacted.
- In-dashboard editor for a project's Claude Code config surfaces — `CLAUDE.md`,
  `settings.json` (env as friendly key/value rows or raw JSON), permissions, hooks,
  MCP servers, subagents, skills, and plugins — at user / project scope (plus local
  for all but subagents and skills), with secret-shaped values masked on the surfaces
  that carry them (settings `env`, MCP). Every write is lost-update-guarded and
  trust-gated; `CLAUDE.md` writes are additionally audit-logged.
- Approximate per-project cost / token badge rolled up from session transcripts.
- Live per-bridge resource metrics (CPU / memory / disk) while a bridge runs.

### Safety

- **Workspace trust** — starting a bridge in an untrusted directory prompts a
  just-in-time confirm that writes the Claude workspace-trust flag before
  spawning. Trusted directories show a green shield and start with no prompt.
- **Auto-enable remote control** — before the first spawn, Clauster
  acknowledges remote control in the runtime user's `~/.claude.json` so a
  detached-stdin bridge isn't stuck on the one-time interactive prompt.
- **Fail-closed auth & networking** — binding beyond loopback is refused unless
  authentication is actually enforced.

### Opt-in extras

- Conversation recap on restart (`claude.resume_recap`).
- Native Interactive Session mode (`claude.launch_mode: pty`).
- Ghost-environment reaper (CLI always available; dashboard gated by
  `reaper.ui_enabled`).
- Background agents (**experimental**) — list / dispatch / stop `claude --bg`
  sessions from a dashboard panel (`/api/agents`); rides Claude Code's agent-view
  research preview, so it may change with the upstream CLI.
- Hosted **Here in the browser** channel (**experimental**) — when the claustrum
  channel is enabled (`claustrum.enabled`, default off), the launch popover gains
  a third option whose sessions are local live-view only: streamed in the
  dashboard but, unlike bridges, never attachable from `claude.ai/code`. See
  [Architecture](architecture.md) and [Configuration](configuration.md).

## Where to next

- [Quickstart](quickstart.md) — your first bridge in a few minutes.
- [Installation](installation.md) — pip / uv, Docker / Compose, running.
- [Configuration](configuration.md) — the complete `clauster.yml` reference.
- [Public API](public-api.md) — the versioned `/api/v1` surface and `clauster api-token` CLI.
- [Security](security.md) — trust model, auth, redaction.
- [Privacy](privacy.md) — what Clauster keeps on disk, and how to purge it.
- [Networking](networking.md) — the loopback / non-loopback auth matrix.
- [MCP server](mcp.md) — the read-only `clauster mcp` stdio server.
- [Architecture](architecture.md) — module map and bridge lifecycle.
- [Operations](operations.md) — monitoring + troubleshooting runbook.

## Stack

Python 3.11+ · FastAPI · Alpine.js + Jinja2 + Tabler · `uv` · `pydantic`.
Developed and CI-gated on Linux; macOS / Windows are in the test matrix.
Apache-2.0 licensed.
