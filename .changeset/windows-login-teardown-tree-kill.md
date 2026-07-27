---
default: patch
---

Cancelling or finishing a Windows login no longer stalls for five seconds and leaks a reader thread when the CLI leaves a child process behind.
