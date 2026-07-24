---
default: patch
---

An MCP server credential carried in a URL query string (`?api_key=…`), a URL fragment, or a stdio `args` element is no longer serialized into `claude mcp add-json`'s argv, where any local user could read it from `ps` / `/proc/<pid>/cmdline`. The keep-secrets-off-argv guard (`entry_needs_direct_write`) only recognized `env`/`headers` values and `scheme://user@host` URLs, which key-name redaction cannot see past; it now fails closed on a URL with a query, userinfo, or fragment component, on a non-empty `args` list, and on an unparseable URL, routing those entries to the direct (non-spawning) writer that produces identical on-disk state. A credential embedded in the URL *path* remains uncovered — every hosted-MCP URL has a path, so that needs a field-allowlist redesign.
