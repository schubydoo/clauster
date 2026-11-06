---
default: patch
---

Direct Sessions survive the claustrum daemon closing an idle connection: Clauster pings it every 90 s, reconnects and reattaches on a drop, and shows a session it cannot recover as an error.
