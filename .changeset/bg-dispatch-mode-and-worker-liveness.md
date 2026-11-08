---
default: patch
---

A background resume now resolves its directory once and uses it for both the permission check and the start (`409` when it cannot be resolved), and a background worker no longer reads as stopped after a system clock correction on Linux.
