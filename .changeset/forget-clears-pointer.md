---
default: patch
---

Forgetting a stopped Interactive Session now also deletes its `bridge-pointer.json` (after a `.bak` backup), so the next launch registers a fresh session instead of silently reattaching an anchor that was archived or deleted out from under it — the root of a bridge coming back idle with no session after a restart (#671).
