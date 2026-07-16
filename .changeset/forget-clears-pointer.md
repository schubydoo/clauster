---
default: patch
---

Forgetting a stopped Interactive Session now also clears its saved pointer, so the next launch registers a fresh session instead of silently reattaching one that was archived or deleted out from under it (#671).
