---
default: build
---

Add the keeper-side live-screen tap (#534): when `claude.pty_screen_enabled` is on (default off, needs the optional `pyte` extra), the PTY keeper renders the bridge's terminal into a redacted, cells-only screen sidecar — strictly best-effort, never affecting the bridge — as groundwork for the live-terminal view; no WebSocket or UI yet.
