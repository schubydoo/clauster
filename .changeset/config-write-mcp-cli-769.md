---
default: minor
---

MCP servers can now be added/removed/edited through Claude Code's own `claude mcp` CLI (secrets travel via env, never the command line), plus project approval enable/disable and a reset-approvals action — all behind the existing config-write gate.
