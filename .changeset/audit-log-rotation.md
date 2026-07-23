---
default: patch
---

`config_audit.log` is now **size-rotated** (#1011): at ~5 MB the current file becomes `config_audit.log.1`, older files shift up, and anything past 5 rotated files is dropped — so the config-write audit trail is bounded at ~30 MB instead of growing unbounded on a long-lived instance. Rotation is best-effort (a rotation error is logged and never blocks the already-best-effort audit append), so a committed config write is never held up by it.
