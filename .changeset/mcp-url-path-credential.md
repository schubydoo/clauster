---
default: major
---

An MCP server URL now reaches `claude mcp add-json`'s world-readable argv only when it is a bare `scheme://host[:port]` origin, so a credential embedded in the URL path can no longer be read from `ps` / `/proc`; every other URL takes the direct file writer, and supplying an OAuth `client_secret` alongside a non-bare URL is now refused with a 422 instead of being saved.
