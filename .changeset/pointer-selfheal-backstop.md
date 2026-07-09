---
default: patch
---

An Interactive Session whose preserved session was archived or deleted while stopped now surfaces as an **error** on restart (instead of a misleading "running" bridge that sits idle with no session), and its stale pointer is cleared so the next launch starts a fresh session (#671). This is the backstop for when the pre-launch check couldn't confirm the anchor (e.g. credentials unavailable): clauster watches a brief grace after a reattach reaches its poll loop, and if the re-adopted session is torn down as gone, it stops the idle bridge and clears the pointer.
