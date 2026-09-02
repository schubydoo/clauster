---
default: patch
---

`doctor`'s `config` row now reports a broken `clauster.yml` by position and key path only. `/api/doctor` no longer echoes the offending YAML line, the rejected scalar, or the parsed mapping. A value YAML cannot construct, or a config nested too deeply, now fails the row instead of tracebacking out of a verb.
