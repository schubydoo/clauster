---
default: minor
---

Fix four bridge-lifecycle defects: keeper sidecars are matched per project instead of by name prefix (a sibling's bridge could be adopted and stopped), a non-positive pid in a sidecar fails closed instead of raising out of startup, `forget` refuses a persisted-only record whose bridge or keeper is still alive, and `max_bridges` counts every live bridge including the spawning project's own.
