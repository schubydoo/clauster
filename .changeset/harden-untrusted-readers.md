---
default: patch
---

Harden two untrusted-input readers against malformed input (both surfaced by new fuzz harnesses): the CSRF/CORS origin check no longer returns a 500 on an `Origin` header with an out-of-range or non-numeric port, and project discovery no longer crashes on a non-dict `~/.claude.json` — each now degrades safely.
