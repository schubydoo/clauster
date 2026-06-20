---
default: patch
---

Harden pty-keeper sidecar parsing so a malformed `keeper_pid`/`bridge_pid` of `true`/`false` no longer resolves to PID 1 (`bool` is an `int` subclass) and is now treated as absent.
