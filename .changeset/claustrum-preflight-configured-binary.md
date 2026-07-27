---
default: patch
---

The claustrum preflight (`doctor`, the session-start panel, and `deps list`) now resolves the binary the same way the daemon spawns it (#1013): a configured `claustrum.binary` — the documented workaround for systemd's minimal PATH — counts as present instead of a permanent false "unavailable" warning, and `deps list` agrees with `doctor` instead of reporting the same binary two different ways.
