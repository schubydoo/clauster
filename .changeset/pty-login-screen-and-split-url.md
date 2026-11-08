---
default: patch
---

An Interactive Session whose `claude` is not logged in now fails with an error that names the login problem instead of showing Running, and the pty keeper no longer captures a truncated session id when the connect URL is split across two reads.
