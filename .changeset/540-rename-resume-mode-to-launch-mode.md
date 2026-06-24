---
default: patch
---

Renamed the confusing `claude.resume_mode` config key to `claude.launch_mode` (#540). The
old name read like a resume on/off toggle, but the field actually picks the bridge launch
mode (`standard` vs `pty`). Existing `clauster.yml` files keep working: the legacy
`claude.resume_mode` key — and the `CLAUSTER_CLAUDE_RESUME_MODE` env var — are still
accepted as deprecated aliases that map to `launch_mode` with a warning (if both the old
and new key are set, the new one wins). Config-editor label and docs updated.
