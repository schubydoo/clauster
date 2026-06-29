---
default: minor
---

Manage permission rules (`settings.json` `permissions` allow/deny/defaultMode) from the dashboard behind the fail-closed `config_write` gate: a structural-only (validate-never-execute) validator + router for project `.claude/settings.json` and user `~/.claude/settings.json`, with type-the-name confirm, path containment, stale-hash guard, and `bypassPermissions` kept behind the existing footgun gate (#689)
