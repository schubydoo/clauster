---
default: patch
---

Fix `clauster install-service` so the generated systemd and launchd units bake a `PATH` (the run-as user's `~/.local/bin` plus the standard system dirs) instead of leaving the service with a minimal default. Clauster propagates its environment to every spawned bridge, so previously a bridge agent couldn't resolve user-local tools (`uv`/`ruff`/`pytest`, etc.) that work fine in an interactive shell. A unit comment now points operators at `claude.path_append` / `claude.env` for shell-managed toolchains (nvm/pyenv/cargo/go).
