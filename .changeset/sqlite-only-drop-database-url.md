---
default: minor
---

Commit to SQLite-only persistence: drop the never-supported `database_url` (Postgres DSN) config key and the arbitrary-DSN passthrough in `db/engine.py`; a leftover `database_url` line in an existing config is silently ignored (additive-only schema), not rejected.
