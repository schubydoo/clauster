---
default: patch
---

Harden Windows state/config file writes: an owner-only ACL on the state dir (keeps `session.secret` private wherever `state_dir` points), an in-process write lock serializing clauster's own concurrent writers, a retry over transient file-sharing violations, and LF (not CRLF) output.
