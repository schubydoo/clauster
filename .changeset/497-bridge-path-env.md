---
default: minor
---

Extend the bridge subprocess `PATH`/env from `clauster.yml` via `claude.path_append` and `claude.env` (both standard and pty modes), so a `claude` session can resolve user-local tools a minimal service `PATH` omits.
