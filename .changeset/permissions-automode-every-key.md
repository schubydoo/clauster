---
default: minor
---

The Permissions tab now edits every documented `permissions` key, and the auto mode classifier rules (`autoMode`) at User scope; it refuses `autoMode` and `defaultMode: "auto"` in project and local settings, where Claude Code ignores them.
