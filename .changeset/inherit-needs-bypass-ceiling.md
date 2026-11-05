---
default: patch
---

The `inherit` permission mode now needs `allow_bypass_permissions: true` on the project, like `bypassPermissions`, because a `bypassPermissions` default in user or managed settings takes effect when no `--permission-mode` flag is passed.
