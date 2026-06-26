---
default: minor
---

Add a live, read-only terminal view for a running pty (true-resume) bridge. A "Live terminal" button on a running pty session opens an auto-scrolling panel that streams the raw terminal frames over a new `/ws/pty-terminal` WebSocket so you can watch what the agent is doing right now without SSH-ing to the host. The PTY keeper mirrors the drained master output to a size-bounded, owner-only capture file (a live frame buffer, truncated to its tail, not an archive), and the stream is auth-gated and ANSI-stripped with every line redacted via `redact.sanitize_line` (no session/env ids or secret shapes leak), exactly like the bridge-log tail. It is strictly read-only — there is no keystroke path back into the PTY — and it fails closed (a non-pty bridge, a missing capture, or an unauthed connect closes with 1008 and leaks no path); terminal output renders via Alpine `x-text`, never `x-html`. Standard multi-session bridges are unaffected.
