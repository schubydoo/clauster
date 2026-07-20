---
default: patch
---

docs: DB-snapshot rollback must delete stale `-wal`/`-shm` sidecars, not keep them (the snapshot is a self-contained `VACUUM INTO` copy)
