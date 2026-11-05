---
default: minor
---

The `inherit` permission mode now requires `allow_bypass_permissions: true` on the project, as `bypassPermissions` does, and is otherwise refused; a background resume now passes `instance_defaults.permission_mode` instead of no permission mode.
