---
default: patch
---

Fix three config-write reads (MCP server approvals, enabled plugins, declared marketplaces) that returned a 500 instead of a 422 when a config file is corrupt.
