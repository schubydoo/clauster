---
default: patch
---

The MCP Server-approvals panel now reflects approvals that live in the settings files (top-level `enabledMcpjsonServers`/`disabledMcpjsonServers`, where `claude mcp add-json --scope local|user` relocates them), so a server approved that way no longer shows as un-approved. A settings-owned approval is shown read-only (marked `settings`), since it can only be changed on the settings surface or via `claude mcp` — the panel no longer offers an approve/reject/unset that would silently revert on reload.
