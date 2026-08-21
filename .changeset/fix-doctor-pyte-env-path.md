---
default: patch
---

`clauster doctor` and the dashboard preflight panel no longer report `extra:pyte` as unavailable when pyte is side-loaded via `CLAUSTER_PYTE_PATH` — the env-var path is now placed on `sys.path` before the probe, matching the managed-deps behaviour. (#1193)
