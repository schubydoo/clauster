---
default: minor
---

Manage MCP servers from the dashboard behind the fail-closed `config_write` gate: a structural-only (validate-never-execute) validator + router for project `.mcp.json` and user `mcpServers`, with type-the-name confirm, path containment, stale-hash guard, and secret redaction inherited from the #347 foundation (#688)
