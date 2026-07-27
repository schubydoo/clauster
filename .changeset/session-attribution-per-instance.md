---
default: major
---

Live sessions are now attributed to the bridge that actually owns them, so a Server Mode bridge no longer lists a project's independent Interactive Sessions as its own; the MCP `list_sessions` tool no longer repeats a session once per bridge, and a bridge's `id` is now its unique instance id (**breaking**: it was the project name — see UPGRADING.md).
