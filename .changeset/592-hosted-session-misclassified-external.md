---
default: patch
---

Fix clauster-spawned hosted (claustrum) sessions being misclassified as `EXTERNAL`/"unmanaged" by the `claude agents --json` cross-check, which also left a stale Active card alongside the Stopped one after Stop — the poll loop now recognizes the hosted registry (by claustrum agent pid, with a workspace-cwd fallback for pre-CT-1 daemons, plus CL-8 orphan survivors) and attributes those sessions to Clauster instead of surfacing them as external.
