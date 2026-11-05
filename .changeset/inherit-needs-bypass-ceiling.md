---
default: patch
---

The `inherit` permission mode now needs `allow_bypass_permissions: true` on the project, like `bypassPermissions`, because a `bypassPermissions` default in user or managed settings takes effect when no `--permission-mode` flag is passed; a background resume now uses `instance_defaults.permission_mode` under the same gate.
