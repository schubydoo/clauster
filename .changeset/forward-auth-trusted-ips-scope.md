---
default: patch
---

Document that forward-auth (header-only) mode trusts the proxy completely, so `trusted_ips` must list only the proxy's own peer IP — an over-broad CIDR or any attacker-reachable host there is a full auth bypass via the unsigned `user_header` (#737)
