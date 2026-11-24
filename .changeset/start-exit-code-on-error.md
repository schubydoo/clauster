---
default: patch
---

`clauster start` now exits 1 with the reason on stderr when the new bridge ends in `error`, so a script that relied on exit 0 will see the failure.
