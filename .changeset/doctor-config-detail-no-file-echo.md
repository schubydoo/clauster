---
default: patch
---

`doctor`'s `config` row now reports a broken `clauster.yml` by position and key path only. `/api/doctor` no longer echoes the offending YAML line or the whole parsed mapping. An unfitting or deeply nested YAML tag now fails the row instead of crashing the command.
