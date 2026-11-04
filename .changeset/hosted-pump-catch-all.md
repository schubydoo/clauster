---
default: patch
---

An unexpected error while reading a Direct Session's output now marks the session as an error, resolves its open permission prompts without allowing them, and shows "stream lost", instead of leaving it silently stuck as running.
