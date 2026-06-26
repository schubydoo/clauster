---
default: build
---

Add the foundation for the read-only live pty terminal view (#534): vendor xterm.js 6.0.0 (self-hosted under `static/vendor/xterm/`, Renovate-pinned) and add the `pyte` server-side terminal-emulation dependency. No user-facing behavior yet — the terminal view, its WebSocket, and the config flag land in later slices.
