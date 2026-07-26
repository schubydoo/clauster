---
default: patch
---

Fix the CLI and MCP server reporting stale, incomplete bridge state, and bridges started from the CLI rendering as EXTERNAL/unmanaged in the dashboard: instance liveness is now persisted and read per instance rather than resolved per project, so every bridge on a project is visible to every process and can be stopped by its own id.
