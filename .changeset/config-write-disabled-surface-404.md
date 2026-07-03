---
default: patch
---

The config-management MCP, permissions, and hooks surfaces now return 404 (not 422) for an unknown scope while config-write is disabled, so a disabled surface can't be fingerprinted.
