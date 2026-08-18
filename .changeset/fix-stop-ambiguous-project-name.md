---
default: patch
---

Fix a bare project name silently resolving to the wrong bridge when a project has more than one instance (#1150). After one ordinary start → stop → start cycle a project keeps two rows, so every by-name surface — `clauster stop`/`logs`/`open`, `GET /api/instances/{name}`, the QR and by-name WebSocket routes, and the `stop_session`/`resume_session` MCP tools — now refuses with an "ambiguous" error listing the candidate instance ids instead of silently picking the last-registered row (which could be a stopped one while a live bridge keeps running). Matches the existing ambiguous id-prefix behaviour (#1099).
