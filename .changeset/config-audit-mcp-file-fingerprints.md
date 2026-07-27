---
default: minor
---

The config-change audit trail now records, for each MCP-server write, **which config files the change actually touched** — by path, SHA-256, and byte size (never the file contents). Because `claude mcp` does Claude Code's own bookkeeping across several files, this makes the subprocess's side effects visible in `config_audit.log` and answers "where did this change land?" without exposing any secret values.
