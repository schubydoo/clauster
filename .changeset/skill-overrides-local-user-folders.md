---
default: patch
---

The Settings tab no longer edits `skillOverrides` (a save that names it is refused with `422`; use the Skills tab), and Local-scope skill visibility also lists your user skill folders when `config_write.allow_user_scope` is on.
