---
default: minor
---

Add an `instance_defaults.verbose` config toggle (default off, editable from the in-app config editor) that passes `--verbose` to spawned standard `claude remote-control` bridges for detailed connection/session logs — observability for diagnosing intermittent bridge disconnects; the pty (flag-form) bridge is intentionally never passed `--verbose` so its live-screen tap stays clean.
