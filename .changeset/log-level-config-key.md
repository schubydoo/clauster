---
default: minor
---

Add a `log_level` config key (`debug` | `info` | `warning` | `error`, also settable as `CLAUSTER_LOG_LEVEL`) that raises Clauster's and uvicorn's server log verbosity, which was previously pinned at `info`.
