---
default: minor
---

The `clauster mcp` server gains write tools — an MCP client can now spawn, stop, and resume bridge sessions (not just observe them). Trust is never auto-granted: `spawn_session` requires an explicit `trust: true` for an untrusted directory.
