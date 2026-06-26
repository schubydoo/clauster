---
default: minor
---

Add an opt-in trusted-header (forward-auth) reverse-proxy mode (`auth.reverse_proxy.require_hmac: false`) so SSO proxies that don't sign a per-request HMAC (Authelia, authentik, Caddy, Traefik, oauth2-proxy) authenticate via `trusted_ips` + `user_header` alone; both modes now require `trusted_ips`, and a `reverse_proxy.enabled` config without it fails fast at startup instead of silently authenticating no one.
