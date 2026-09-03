---
default: security
---

`doctor`'s `config` row now reports a broken `clauster.yml` by position and key path only. It never echoes the offending YAML line, the rejected scalar, or the parsed mapping.
