# Clauster

A self-hosted web UI for spawning and managing Claude Code `remote-control`
bridges into arbitrary project directories on a remote host (NAS, homelab box),
accessed from any browser or the Claude mobile app.

Anthropic's first-party tools assume terminal access on the host to spawn a
bridge in a new directory. Clauster fills that gap: a browser-based dispatcher
of `claude remote-control` instances on a remote machine. Once spawned, you
attach via `claude.ai/code` or the mobile app.

> Status: **v0.1 in development.** Loopback-only, no auth (lands in v0.2).

## v0.1 features

1. Project discovery — one card per directory under `projects_root`.
2. Per-project bridge status.
3. Start bridge.
4. Stop bridge.
5. URL display ("Open in Claude").
6. Bridge log tail.

## Quick start (dev)

```sh
uv sync --extra dev
cp clauster.yml.example clauster.yml   # edit projects_root
uv run clauster
```

Then open <http://127.0.0.1:7621>.

## Stack

Python 3.11+ · FastAPI · Alpine.js + Jinja2 · `uv`. Apache-2.0 licensed. No telemetry, ever.
