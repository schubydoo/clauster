---
default: patch
---

De-duplicate the SHA-256 hash helper so `config_editor` delegates to the single canonical `config_write.hash_bytes` (no behavior change).
