---
default: patch
---

An empty `claude_session_uuid` in a corrupt `hosted_state.json` record no longer blocks a hosted session from learning its real session id. Before, that row lost `--resume` for the life of the process.
