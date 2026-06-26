---
default: minor
---

Add an in-app "Restart Clauster" action to the config editor that re-execs the process in place (`os.execv`, same PID, reloads config) so a saved config change can be applied without dropping to a shell; gated behind the existing restart-impact confirmation and exposed via an auth-gated `POST /api/restart`.
