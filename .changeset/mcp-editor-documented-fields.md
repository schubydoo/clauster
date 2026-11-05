---
default: minor
---

The MCP config editor now accepts every server field Claude Code documents or its CLI reads (`ws`, `streamable-http`, `headersHelper`, `oauth`, `timeout`, `alwaysLoad`, `tools`, `toolPermissions` and more), masks every `env`/`headers` value and `headersHelper` on read, and refuses what Claude Code rejects or ignores (dotted or reserved names, `transport`, `env` on a remote server).
