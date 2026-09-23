---
default: patch
---

Fix three config-write reads (MCP server approvals, enabled plugins, declared marketplaces) that returned a 500 when a config file is corrupt (now 422) or unreadable (now 400).
