---
default: patch
---

Fix the Windows owner-only ACL so a stuck `icacls` no longer fails the state write — it now logs a warning and proceeds on the inherited permissions, restoring the best-effort contract.
