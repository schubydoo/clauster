# Networking

Clauster decides whether a given bind is allowed by asking one question: **will
authentication actually gate requests?** This is enforced at config-load time by
the `_loopback_or_authed` model validator in `config.py`, backed by the shared
`_missing_enforced_auth` helper.

## Loopback vs non-loopback

The loopback hosts are `127.0.0.1`, `::1`, and `localhost`. Binding to any of
these **never requires auth** — the dashboard is only reachable from the machine
itself.

Binding to anything else (a LAN IP, `0.0.0.0`, a public address) is a
**non-loopback bind** and is refused unless authentication is *actually
enforced*. "Enforced" means:

```text
auth.enabled == true  AND  (auth.password_required  OR  auth.reverse_proxy.enabled)
```

The explicit `auth.allow_unauthenticated_network` opt-out lets you bypass that
requirement on a trusted LAN — the config validator permits it, and
`clauster doctor` downgrades it to a warning rather than an error.

## The auth / networking matrix

| `host` | `auth.enabled` | method (`password_required` or `reverse_proxy.enabled`) | `allow_unauthenticated_network` | Result |
| --- | --- | --- | --- | --- |
| loopback | any | any | any | ✅ Starts (loopback never needs auth) |
| non-loopback | `false` | — | `false` | ❌ **Refused** — silent open door |
| non-loopback | `true` | none set | `false` | ❌ **Refused** — switch on but nothing enforces it |
| non-loopback | `false`/`true` | password set but `enabled: false` | `false` | ❌ **Refused** — password without the master switch is a no-op |
| non-loopback | `true` | `password_required` (+ hash) | any | ✅ Starts (password login) |
| non-loopback | `true` | `reverse_proxy.enabled` | any | ✅ Starts (proxy trust) |
| non-loopback | any | any | `true` | ⚠️ Starts (explicit unauthenticated opt-out; doctor warns) |

A second validator independently refuses to start when `password_required` is
set but `password_hash` is empty — regardless of host — because that would lock
everyone out or be silently skipped.

## Password auth on a non-loopback bind

```yaml
projects_root: ~/code
host: 0.0.0.0
auth:
  enabled: true
  password_required: true
  password_hash: "$argon2id$v=19$..."   # clauster hash-password
  cookie_secure: always                 # if there is no TLS-terminating proxy
```

!!! warning "Plain-http cookie warning"
    With password auth on and no TLS proxy, the session cookie may ship without
    the `Secure` flag and be sniffable on the wire. Clauster warns about this at
    startup. Put it behind https / a TLS proxy, or set `cookie_secure: always`.

## Behind a reverse proxy

Set `root_path` if Clauster is served under a sub-path, and use
`auth.reverse_proxy` for proxy-terminated auth:

```yaml
projects_root: ~/code
host: 0.0.0.0
root_path: /clauster            # if mounted under a sub-path
auth:
  enabled: true
  allowed_origins:
    - https://clauster.example.com
  reverse_proxy:
    enabled: true
    user_header: Remote-User
    shared_secret_header: X-Proxy-Auth
    shared_secret: "<hmac-key-the-proxy-signs-with>"
    trusted_ips:
      - 10.0.0.2                # the proxy's peer IP
    hmac_window_seconds: 60     # clock-skew / replay window
```

The proxy authenticates the user, sets `user_header`, and signs
`shared_secret_header` with the shared HMAC key. Clauster verifies the request
came from a `trusted_ips` peer **and** that the HMAC is valid within
`hmac_window_seconds`. With `reverse_proxy.enabled` + `auth.enabled`, no password
hash is required.

When a trusted proxy terminates TLS and reports `X-Forwarded-Proto=https`,
`cookie_secure: auto` correctly marks the session cookie `Secure`.

## Docker

The container binds `0.0.0.0` internally, so it **requires enforced auth to
start** — the bundled `compose.yaml` sets `CLAUSTER_AUTH_ENABLED=true` and
`CLAUSTER_AUTH_PASSWORD_REQUIRED=true` and expects a
`CLAUSTER_AUTH_PASSWORD_HASH`. See [Installation](installation.md#docker).
