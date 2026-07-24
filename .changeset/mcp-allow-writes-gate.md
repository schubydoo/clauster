---
default: major
---

The `clauster mcp` write tools (`spawn_session` / `stop_session` / `resume_session`) are now gated behind a new `mcp.allow_writes` config key that defaults **off**, so the local-privileged, unauthenticated stdio MCP surface is read-only by default and its `--help`/banner no longer understate its capability (#1010); set `mcp.allow_writes: true` to restore the #950 write tools.
