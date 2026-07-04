---
default: patch
---

`/healthz` now reports whether the runtime `claude` account is authenticated (via `claude auth status`, so it covers OAuth, API-key, and helper logins alike) as `claude_login_ok` / `claude_login_method`, and the dashboard shows a header pill when it's logged out, so a stale login surfaces before a bridge silently hangs at "Starting".
