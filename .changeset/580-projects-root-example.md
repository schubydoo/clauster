---
default: patch
---

`clauster.yml.example` now uses `projects_root: ~/code` (matching every prose doc) instead of `/srv/projects`, which contradicted the docs and hard-failed validation if copied as-is on a box without that directory.
