---
default: patch
---

Bridge runner: fix a race where `stop()` could orphan a bridge. `stop()` read `bridge_pid` without holding the per-project spawn lock, so a stop arriving while `spawn()` was suspended in `to_thread(_popen)` would see `bridge_pid=None`, mark the instance STOPPED, and return — leaving the freshly-spawned bridge running but untracked. `stop()` now takes the same lock `spawn()`/`forget()`/`resume()` use, so it waits for any in-flight spawn to publish its pid before reading it.
