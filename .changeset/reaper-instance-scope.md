---
default: patch
---

Stop the environment reaper archiving a live bridge owned by another clauster instance or OS user: it filtered an account-wide environment list against an instance-scoped live set, so any bridge outside this instance's `projects_root` read as a leftover and could be archived mid-session — such an environment is now unattributable and skipped.
