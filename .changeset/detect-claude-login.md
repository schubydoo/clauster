---
default: patch
---

The dashboard now shows a header pill when the runtime `claude` account is logged out, and `/healthz` reports its auth state (`claude_login_ok` / `claude_login_method`, covering OAuth, API-key, and helper logins) — so a stale login surfaces before a bridge silently hangs at "Starting".
