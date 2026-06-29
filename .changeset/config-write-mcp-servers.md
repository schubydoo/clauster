---
default: minor
---

Add a gated `config_write` API for MCP servers behind the fail-closed gate: a structural-only (validate-never-execute) validator + router for project `.mcp.json` and user `mcpServers`, with type-the-name confirm, path containment, stale-hash guard, and secret redaction inherited from the #347 foundation — no dashboard UI yet (#688)
