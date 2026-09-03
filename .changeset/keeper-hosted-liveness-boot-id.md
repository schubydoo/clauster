---
default: patch
---

Keeper sidecars and hosted-session rows now record a boot id. A host clock step no longer makes a live Interactive Session keeper or hosted agent look dead across a reboot. A keeper or hosted session started before this release is unchanged until its next spawn.
