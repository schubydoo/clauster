---
default: patch
---

An Interactive Session whose preserved session was archived or deleted while stopped now surfaces as an **error** on restart — instead of a misleading "running" bridge that sits idle with no session — and its stale pointer is cleared so the next launch starts fresh (#671). This is the backstop for when the pre-launch check can't confirm the session (e.g. credentials unavailable).
