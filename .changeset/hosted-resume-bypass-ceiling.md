---
default: security
---

Fix hosted resume ignoring the project's `allow_bypass_permissions` ceiling: a session stored in bypass mode now gets 403 when the current configuration forbids bypass.
