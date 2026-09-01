---
default: patch
---

An empty `claude_session_uuid` in a corrupt `hosted_state.json` record no longer blocks a hosted session from ever learning its real session id, which had cost that row `--resume` for the process lifetime.
