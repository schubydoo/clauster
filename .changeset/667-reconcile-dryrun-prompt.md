---
default: patch
---

`clauster config reconcile --dry-run` is now non-interactive — it previously ran the per-key prompt before the dry-run guard and blocked on a terminal; it now prints the plan and writes nothing without prompting.
