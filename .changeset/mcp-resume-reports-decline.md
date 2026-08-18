---
default: patch
---

Fix the MCP `resume_session` tool reporting a cap-declined resume as success (#1148): the one-live-standard-bridge-per-project cap hands back the already-live bridge, so a declined resume answered `resumed: true` with a bridge that was never revived. The tool now reads the full outcome (new `ClausterEngine.resume_detailed`) and answers `resumed: false` with a `reason`, mirroring `POST /api/instances/{id}/resume` (#1145).
