---
default: patch
---

Fix four bridge-lifecycle defects: keeper sidecars now match per project rather than by name prefix, a non-positive sidecar pid fails closed instead of raising out of startup, `forget` refuses a persisted-only record whose bridge or keeper is still alive, and `max_bridges` counts every live bridge including the spawning project's own.
