---
default: minor
---

The Subagents editor now checks each inline `mcpServers` definition with the MCP tab's field rules and refuses a reserved name, an item with two servers, or a repeated inline name, and it converts a stored `transport` or remote `env` on load so the next save fixes it.
