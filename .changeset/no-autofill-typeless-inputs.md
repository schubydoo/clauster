---
default: patch
---

The launch popover's First prompt and custom session name fields no longer get autofilled by a password manager — they were the only opt-out inputs without an explicit `type`, which is enough for a manager to skip the opt-out entirely.
