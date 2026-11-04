---
default: patch
---

An unexpected error while reading a Direct Session's output now marks it as an error, resolves its open permission prompts without allowing them, shows "stream lost", and ends the agent, instead of leaving the session silently stuck as running.
