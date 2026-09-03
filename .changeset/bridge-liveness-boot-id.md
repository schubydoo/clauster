---
default: patch
---

When the host clock is stepped by any amount, a bridge started after this release keeps its Running card. A bridge record left by a previous boot is no longer mistaken for a live process on a recycled PID. A bridge started before this release is unchanged until its next spawn.
