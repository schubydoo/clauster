---
default: patch
---

Reject a boolean `bridge_pid` in a pty keeper sidecar so a `"bridge_pid": true` value no longer persists PID 1 (alive on every host, making the row read live forever). (#1182)
