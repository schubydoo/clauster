---
default: patch
---

Serialize hosted-session lifecycle ops (stop/resume/forget/kill_orphan) under a per-id lock so two concurrent resumes can't both spawn a process for one conversation, validate an unknown hosted `permission_mode` as a 422 (parity with the bridge channel), and drain in-flight notify sends on shutdown so a pending task isn't GC-cancelled at exit
