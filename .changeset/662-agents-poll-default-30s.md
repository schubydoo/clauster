---
default: patch
---

Lower the default `claude.agents_json_poll_interval_seconds` from 300 to 30 so session liveness (the transcript live badge, active-session zone) and crash detection refresh within ~30s instead of up to 5 minutes.
