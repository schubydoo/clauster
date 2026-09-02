---
default: patch
---

Winding down a lingering pty keeper now checks its boot-relative start time, so a host clock correction mid-shutdown no longer leaves the keeper running.
