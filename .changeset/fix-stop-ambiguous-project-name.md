---
default: patch
---

Fix a bare project name silently resolving to the wrong bridge when a project has more than one instance (#1150) — after one start → stop → start cycle a project keeps two rows. Every by-name surface (`clauster stop`/`logs`/`open`, `GET /api/instances/{name}`, the QR and by-name WebSocket routes, and the `stop_session`/`resume_session` MCP tools) now refuses with an "ambiguous" error listing the candidate instance ids, matching the id-prefix behaviour (#1099).
