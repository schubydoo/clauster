---
default: patch
---

Browser notifications now prompt for permission the moment you enable the channel instead of only after a reload, and a failed bridge resume only raises the "reconnect failed" notification when the bridge genuinely could not restart (not when the session was already gone or the request was invalid).
