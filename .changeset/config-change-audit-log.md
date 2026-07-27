---
default: minor
---

Every config-write is now recorded to a single `config_audit.log` audit trail — one JSON line per change across every surface (CLAUDE.md, settings, permissions, hooks, MCP, approvals, subagents, skills, plugins, marketplaces), capturing the surface, scope, target file, action, actor, and the top-level key names touched (never the values). Generalises the previous CLAUDE.md-only edit audit so a config change can be traced to where it landed.
