---
default: patch
---

The MCP Server-approvals panel now reflects approvals that live in the settings files (top-level `enabledMcpjsonServers`/`disabledMcpjsonServers`, where `claude mcp add-json --scope local|user` relocates them), so a server approved that way no longer shows as un-approved.
