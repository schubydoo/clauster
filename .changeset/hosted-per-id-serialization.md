---
default: patch
---

Stop hosted sessions from spawning duplicate processes when resumed twice at once, reject an unknown `permission_mode` with a clear 422, and finish pending notifications cleanly on shutdown.
