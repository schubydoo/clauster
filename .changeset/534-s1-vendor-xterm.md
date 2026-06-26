---
default: build
---

Vendor xterm.js 6.0.0 (self-hosted under `static/vendor/xterm/`, Renovate-pinned) as the front-end foundation for the read-only live pty terminal view (#534), and document the front-end vendoring convention in CONTRIBUTING. No user-facing behavior yet — the terminal view, its WebSocket, and the (default-off) config flag land in later slices.
