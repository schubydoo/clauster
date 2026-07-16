---
default: patch
---

Launching an Interactive Session now pre-checks the preserved session and, if it was archived or deleted, clears the stale pointer so the launch starts fresh instead of reattaching a dead session and coming back idle (#671). Best-effort — it never blocks a launch, and leaves the pointer untouched on any uncertainty.
