---
default: patch
---

Live sessions are now attributed to the bridge that actually owns them, so a Server Mode bridge no longer lists a project's independent Interactive Sessions as its own and the MCP `list_sessions` tool no longer repeats a session once per bridge on that project.
