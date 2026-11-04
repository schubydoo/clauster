---
default: patch
---

When reading a Direct Session's output fails, the session now ends visibly: it is marked as an error, its open permission prompts resolve without being allowed, the view shows "stream lost", and the agent is killed so that a Resume does not run beside it.
