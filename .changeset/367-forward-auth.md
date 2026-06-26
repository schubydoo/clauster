---
default: minor
---

Add an opt-in trusted-header (forward-auth) reverse-proxy mode: set `auth.reverse_proxy.require_hmac: false` and a request from a `trusted_ips` peer carrying `user_header` authenticates on the header alone, so SSO proxies that don't sign a per-request HMAC (Authelia, authentik, Caddy `forward_auth`, Traefik, oauth2-proxy) work out of the box. The HMAC path stays the default and is unchanged; both modes require `trusted_ips` (the peer-IP allowlist is the first gate, so it fails closed without it), never lets the login rate-limiter key on the forgeable bare header (a forged-username flood collapses to the shared-IP global backoff), and ships worked Caddy+Authelia and oauth2-proxy recipes in `docs/networking.md`.

Upgrade note: a reverse_proxy.enabled config now fails fast at startup unless trusted_ips is set (the peer-IP gate, required in both HMAC and header-only modes), plus a shared_secret in HMAC mode — previously such a config could start and silently authenticate no one.
