---
default: patch
---

Launching an Interactive Session now pre-checks the preserved bridge pointer's anchor session (via the `/v1/code/sessions` API) and, if it was archived or deleted, clears the stale pointer so the launch starts a fresh session instead of reattaching a dead one and coming back idle with no session (#671). The check is best-effort — it never blocks or fails a launch, and leaves the pointer untouched on any uncertainty.
