---
default: minor
---

Auto-snapshot `clauster.db` via `VACUUM INTO` before a pending migration only, retaining the last 5 under `state_dir/backups/`.
