---
default: patch
---

Config and trust JSON writes now take their advisory lock on a file under `<state_dir>/locks/` instead of a `<file>.lock` sidecar, so editing a project's settings no longer drops a lock artifact inside its git-tracked tree. ([#1171](https://github.com/schubydoo/clauster/issues/1171))
