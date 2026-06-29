---
default: minor
---

Add a gated `config_write` API for permission rules (`settings.json` `permissions` allow/deny/defaultMode) behind the fail-closed gate: a structural-only (validate-never-execute) validator + router for project `.claude/settings.json` and user `~/.claude/settings.json`, with type-the-name confirm, path containment, stale-hash guard, and `bypassPermissions` kept behind the existing footgun gate — no dashboard UI yet (#689)
