---
default: minor
---

Add an `instance_defaults.verbose` config toggle (default off, editable from the in-app config editor) that passes `--verbose` to spawned standard `claude remote-control` bridges in every spawn mode (same-dir/worktree/session) for detailed connection/session logs; the pty (flag-form) bridge is intentionally never passed `--verbose` so its live-screen tap stays clean.
