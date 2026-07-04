---
default: patch
---

The per-project readiness check now warns when a committed `.mcp.json` has servers pending approval, since an interactive (pty) launch hangs invisibly at claude's MCP-approval prompt otherwise — resolve them in Server approvals first.
