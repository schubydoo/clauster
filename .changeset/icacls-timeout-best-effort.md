---
default: patch
---

Fix the Windows owner-only ACL so a hung `icacls` (30s `subprocess` timeout → `TimeoutExpired`, which is not an `OSError`) degrades to a logged warning and proceeds on the inherited ACL, instead of failing the state write — restoring the best-effort contract.
