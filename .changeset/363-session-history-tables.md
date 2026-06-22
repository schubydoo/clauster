---
default: minor
---

Record bridge/session lifecycle events (spawned, ready, ended, crashed) with mode and an end-of-session cost/token snapshot to a persistent session-history table, with per-project "last used / total cost" rollups readable from the DB.
