---
default: patch
---

`clauster doctor` now reports the detected claustrum daemon version and warns — advisorily, never as a failure — when it can't be confirmed at or above the release clauster pins (#1013): an unstamped/dev build or an older release is surfaced rather than silently assumed compatible, and a managed `deps/bin` install shadowed by a different `PATH`/configured binary now raises a `binary:claustrum:shadow` warning so you know which copy actually runs. Completes the claustrum-preflight follow-up (Bug 3–5).
