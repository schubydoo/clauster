---
default: build
---

Add the `/ws/pty-screen` WebSocket endpoint (#534): it polls the keeper's redacted screen sidecar and streams cells-only frames (de-duped by seq, never raw ANSI) to the browser, gated on a pty bridge with `claude.pty_screen_enabled` on; groundwork for the live-terminal view — no UI yet.
