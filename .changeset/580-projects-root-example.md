---
default: patch
---

`clauster.yml.example` now ships `projects_root: ~/code`, matching the `~/code` used in every prose doc (README, quickstart, configuration, networking) and the file's own `~/.clauster` style — instead of `/srv/projects`, which contradicted the docs and hard-failed validation if copied as-is on a box without that directory. The comment is reworded to read as an explicit "edit me" placeholder.
