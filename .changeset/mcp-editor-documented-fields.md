---
default: minor
---

The MCP config editor now accepts every server field Claude Code documents (`ws` servers, `headersHelper`, `oauth`, `timeout`, `alwaysLoad`, `bareElicitationCapability`, `streamable-http`), masks every `env`/`headers` value and `headersHelper` on read, and refuses server names Claude Code rejects.
