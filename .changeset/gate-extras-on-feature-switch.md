---
default: patch
---

`clauster doctor` and the dashboard preflight panel no longer nag about an optional dependency for a feature you've switched **off** (#1016): the `extra:` rows now gate on their feature switch — `pyte`/`pywinpty` on `claude.pty_screen_enabled`, `apprise` on `notifications.enabled` — mirroring how the binary rows already gate on `claustrum.enabled`. So the panel read before every session isn't cluttered with permanent, un-actionable warnings for capabilities you deliberately don't use; a dep only surfaces once its feature is turned on.
