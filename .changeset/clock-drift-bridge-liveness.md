---
default: patch
---

Correcting the host clock no longer makes a running bridge report Stopped or Crashed, blank its CPU/RAM chip, or — via the phantom-prune — delete its own still-live card: bridge liveness now pairs the wall-clock create-time with a boot-relative start time that NTP cannot move, on spawn, pointer-walk, adopt and pty-keeper reattach alike, and the prune no longer treats a bridge Clauster is holding as evidence of an unmanaged one.
