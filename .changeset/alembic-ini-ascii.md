---
default: patch
---

The frozen binary now completes its first-run database migration under a non-UTF-8 locale (#1015): `alembic.ini` was ASCII-cleaned so `configparser` no longer crashes on an em-dash when the binary starts with no `LANG`/`LC_ALL` against a fresh database.
