---
default: minor
---

Add the read-only live PTY terminal view (#534, completes the epic): when `claude.pty_screen_enabled` is on, each pty bridge gets a "Live terminal" button that streams the keeper's redacted, cells-only screen frames over `/ws/pty-screen` into an xterm.js terminal (auth-gated, never raw ANSI); the flag is now toggleable in the in-app config editor.
