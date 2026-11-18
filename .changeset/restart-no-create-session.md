---
default: minor
---

Add `instance_defaults.create_session_on_restart`: set it to `false` and a Restart of a standard bridge passes `--no-create-session-in-dir`, so no session starts until you open one.
