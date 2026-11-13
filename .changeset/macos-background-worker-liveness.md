---
default: patch
---

On macOS a running background agent now reads as alive, so Stop sends it the orderly double SIGINT instead of reporting no live worker.
