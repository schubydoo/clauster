---
default: minor
---

Remove the per-launch sandbox option: a `sandbox` value is now ignored with a warning, the instance JSON drops `sandbox_mode`, and to sandbox sessions you set `sandbox.enabled` in a Claude Code settings file.
