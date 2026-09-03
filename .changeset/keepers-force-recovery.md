---
default: patch
---

`clauster keepers --kill <pid> --force` now stops a live keeper that the normal cleanup spared on a still-carded project. It runs the same PID-reuse identity check first, and is the recovery path for a keeper no other automated stop can reach.
