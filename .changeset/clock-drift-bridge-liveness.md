---
default: patch
---

Bridge liveness now pairs the wall-clock start time with a boot-relative one that NTP cannot move. A host clock correction no longer marks a running bridge Stopped, blanks its CPU/RAM chip, or deletes its card.
