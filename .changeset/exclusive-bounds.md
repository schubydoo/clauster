---
default: patch
---

Fix the config editor accepting a bounded value it then rejects on save with a `422`. It now enforces the bound up front, including an exclusive endpoint that must be exceeded, not merely met.
