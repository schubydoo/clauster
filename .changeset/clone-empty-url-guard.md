---
default: patch
---

Disable the Clone submit button while the Git URL is empty (and soften the backend's missing-URL 422) so an empty clone no longer surfaces a raw error.
