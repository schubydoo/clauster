---
default: patch
---

Harden Windows state/config file writes: a best-effort owner-only ACL on the state dir, an in-process write lock serializing clauster's own concurrent writers, a retry over transient file-sharing violations, and LF (not CRLF) output.
