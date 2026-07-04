---
default: patch
---

`/healthz` now reports `claude_login_ok` / `claude_login_expires_at` for the runtime `claude` account, and the dashboard shows a header pill when it's logged out or expired, so a stale login surfaces before a bridge silently hangs at "Starting".
