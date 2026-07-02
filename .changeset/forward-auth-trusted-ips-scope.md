---
default: patch
---

Document that in forward-auth (header-only) mode, `trusted_ips` must list only your proxy's own IP — a broader range lets anyone reachable there bypass authentication.
