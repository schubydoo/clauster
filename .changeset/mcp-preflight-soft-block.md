---
default: patch
---

Launching an Interactive Session in a project with unapproved committed `.mcp.json` servers now soft-blocks with an error-styled confirm (it would otherwise hang at claude's MCP approval prompt and never connect): a "Resolve in Server approvals" button, a "Launch anyway" override, and block-by-default. Resolving the servers refreshes the check so the next launch proceeds.
