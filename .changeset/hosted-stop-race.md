---
default: patch
---

Fix a `HostedManager.stop()` KeyError (an unmapped 500) when a concurrent forget/resume pops the hosted session's registry row during the stop grace window — re-fetch after the await and surface a clean 404 instead.
