---
default: patch
---

Keeper liveness now pairs the wall-clock start time with a boot-relative one that NTP cannot move. A host clock correction no longer lets Forget delete the record of a running Interactive Session.
