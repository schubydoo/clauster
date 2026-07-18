---
default: minor
---

The config editor now has an **Advanced settings** panel (#978): when config-write is enabled, the `clone.*` and `webhooks.*` scalars are editable from the dashboard behind a step-up re-auth — re-enter your password to unlock a short window, edit, and save through the same "restart to apply" flow. A wrong password shows an inline error and keeps the panel locked; the unlock window expiring mid-edit re-prompts rather than silently failing. Network bind, authentication, and TLS stay file/CLI-managed.
