---
default: patch
---

pty bridges: the keeper now waits on its PTY with `poll()` instead of `select()`. `select.select()` raises "filedescriptor out of range in select()" once the PTY master file descriptor reaches FD_SETSIZE (1024) — which a long-lived Clauster managing many bridges/keepers (or running on a busy host with many open fds) could hit, crashing the keeper's read loop. `poll()` has no such ceiling.
