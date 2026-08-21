---
default: patch
---

A bridge left in `error` status (e.g. a Resume/spawn that failed to launch) no longer renders in neither dashboard zone and vanishes — it now stays in the Recent zone as a "Failed to start" card with its `error_detail` on screen, so a failed launch fails visibly instead of silently. (#1149)
