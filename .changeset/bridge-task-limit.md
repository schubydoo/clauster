---
default: minor
---

Add `instance_defaults.bridge_task_limit` (off by default; needs Linux, systemd and `loginctl enable-linger`, else bridges start unscoped and `clauster doctor` warns): each bridge starts in its own systemd user scope with this task limit.
