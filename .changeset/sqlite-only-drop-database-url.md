---
default: minor
---

Commit to SQLite for storage and remove the unsupported `database_url` (Postgres) config key; a leftover `database_url` in your config is now ignored rather than rejected.
