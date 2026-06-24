---
default: patch
---

Security hardening (batch). The reverse-proxy `trusted_ips` allowlist is now validated as IP/CIDR at load — a malformed entry fails fast instead of silently never matching at runtime. The opt-in webhook SSRF guard (`webhooks.block_private_targets`) now **resolves DNS hostnames** and drops a URL whose name points at an internal IP, closing the hostname-bypass (a rebinding domain that re-resolves between the check and the POST remains an acknowledged TOCTOU residual). The clone `allow_private_hosts` description now states the field semantics rather than the default's effect. Internal hardening: direct unit tests for the hosted-stream redactor, a parity test pinning that every WebSocket endpoint enforces the same auth gate, and an inline note documenting why the session cookie's `SameSite=Lax` is safe (state changes are independently Origin-gated).
