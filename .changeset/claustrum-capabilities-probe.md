---
default: patch
---

Probe the claustrum daemon with `server.capabilities` instead of the `server.version` method that claustrum v1.10 removes, so the connection keeps working across current and future daemon releases.
