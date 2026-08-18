---
default: patch
---

Fix the MCP `resume_session` tool reporting a declined resume as success (#1148). A standard bridge is capped at one live per project, and the cap is enforced by handing back the already-live bridge rather than raising — so a resume the cap declined answered `resumed: true` with a bridge that was never revived, which an agent then acts on. The tool now reads the full outcome (new `ClausterEngine.resume_detailed`) and answers `resumed: false` with a `reason`, mirroring what `POST /api/instances/{id}/resume` already reports (#1145).
