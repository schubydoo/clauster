---
default: patch
---

The Direct Session (hosted) lifecycle API endpoints — stop, resume, forget, and message — now accept a session's `instance_id` in addition to its `claustrum_process_id`, so an API client can address a session by whichever id it already holds.
