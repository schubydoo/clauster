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
auth.enabled == true  AND  (auth.password_required  OR  auth.reverse_proxy.enabled  OR  auth.api_token_hash)
```

Any one of the three methods satisfies the requirement. `password_required`
gates the browser login, `reverse_proxy.enabled` trusts a proxy-terminated
identity, and `api_token_hash` (Bearer-token auth for headless/API clients,
minted with `clauster hash-token`) lets a caller present `Authorization: Bearer
<token>` instead of a browser session.

The explicit `auth.allow_unauthenticated_network` opt-out lets you bypass that
requirement on a trusted LAN — the config validator permits it, and
`clauster doctor` downgrades it to a warning rather than an error.

## The auth / networking matrix

| `host` | `auth.enabled` | method (`password_required`, `reverse_proxy.enabled`, or `api_token_hash`) | `allow_unauthenticated_network` | Result |
| --- | --- | --- | --- | --- |
| loopback | any | any | any | ✅ Starts (loopback never needs auth) |
| non-loopback | `false` | — | `false` | ❌ **Refused** — silent open door |
| non-loopback | `true` | none of the three set | `false` | ❌ **Refused** — switch on but nothing enforces it |
| non-loopback | `false`/`true` | password set but `enabled: false` | `false` | ❌ **Refused** — password without the master switch is a no-op |
| non-loopback | `true` | `password_required` (+ hash) | any | ✅ Starts (password login) |
| non-loopback | `true` | `reverse_proxy.enabled` | any | ✅ Starts (proxy trust) |
| non-loopback | `true` | `api_token_hash` set | any | ✅ Starts (Bearer-token auth) |
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

## Native HTTPS (built-in TLS)

Instead of an external TLS proxy, Clauster can terminate HTTPS itself — point it
at an existing certificate + key and uvicorn serves TLS directly. This is the
simplest way to get a **secure context** (required for browser features like Web
Notifications on a LAN IP) when you don't want a reverse proxy or `tailscale
serve`.

```yaml
projects_root: ~/code
host: 0.0.0.0
auth:
  enabled: true
  password_required: true
  password_hash: "$argon2id$v=19$..."   # clauster hash-password
tls:
  cert_file: /etc/clauster/tls/fullchain.pem   # PEM cert (chain)
  key_file: /etc/clauster/tls/privkey.pem      # PEM private key
```

Both paths are validated **fail-closed, twice** — at config-load and again at
server start — for existence, readability, and that they resolve to an absolute
file (any `..` is collapsed). If TLS is configured but a file is missing or
unreadable, Clauster **aborts startup with a clear error** rather than silently
falling back to plain HTTP. The key material is never logged.

With native TLS the connection is `https`, so `auth.cookie_secure: auto` already
marks the session cookie `Secure` — no `cookie_secure: always` needed, and the
plain-http cookie warning above is suppressed. The bind/auth rules are unchanged:
HTTPS does not relax the non-loopback "enforced auth" requirement.

!!! note "Scope — cert provisioning is out of scope"
    This wires an **existing** cert + key into uvicorn only. Self-signed-cert
    generation and ACME/Let's Encrypt automation are **not** part of this feature —
    obtain the cert with your own tool (mkcert, `openssl`, `certbot`, your CA) and
    point `tls.cert_file`/`tls.key_file` at it.

!!! warning "`tls` is file/CLI-managed only"
    `tls.cert_file`/`tls.key_file` are structural filesystem paths, so — like the
    bind host and secret hashes — they are **not** editable from the in-app config
    editor. Set them in `clauster.yml` (or via `CLAUSTER_TLS_CERT_FILE` /
    `CLAUSTER_TLS_KEY_FILE`).

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

## Trusted-header (forward-auth) mode

The HMAC recipe above is the higher-assurance default. Many SSO proxies, though
— Authelia, authentik, Caddy `forward_auth`, Traefik, oauth2-proxy — authenticate
the user and forward a `Remote-User` header but **do not sign a per-request HMAC**.
For those, set `reverse_proxy.require_hmac: false`: a request from a `trusted_ips`
peer carrying `user_header` then authenticates on the header alone — no HMAC needed.

```yaml
host: 0.0.0.0
auth:
  enabled: true
  allowed_origins:
    - https://clauster.example.com
  reverse_proxy:
    enabled: true
    require_hmac: false         # forward-auth: trust user_header from a trusted peer
    user_header: Remote-User
    trusted_ips:
      - 10.0.0.2                # the proxy's peer IP — REQUIRED in this mode
```

!!! danger "The header is only as trustworthy as the proxy"
    With `require_hmac: false` the `user_header` is **unsigned and forgeable** by
    anyone who can reach a `trusted_ips` peer. Keep `require_hmac: true` (the
    default) whenever your proxy can sign an HMAC; only drop to header-only mode
    under the two conditions below.

Use header-only mode only when **both** hold:

- the proxy is the **sole** network route to clauster (clauster is not reachable
  directly), and
- the proxy **strips `user_header` from every inbound client request** before
  re-adding its own authenticated value (otherwise a client can forge the user).

`trusted_ips` is **mandatory** in this mode — clauster refuses to start without it.
The login rate-limiter never keys on the bare header: a forged-username flood from
a trusted IP collapses to the shared-IP global backoff, so it can't mint a fresh
per-user login budget.

### Recipe — Caddy `forward_auth` + Authelia

Caddy delegates each request to Authelia, then forwards Authelia's `Remote-User`
to clauster. Clauster trusts it because Caddy is the only `trusted_ips` peer.

```caddyfile
clauster.example.com {
    # Defence-in-depth: strip any client-supplied identity headers before
    # forward_auth runs, so only Authelia's values reach clauster.
    request_header -Remote-User
    request_header -Remote-Groups
    request_header -Remote-Email
    forward_auth authelia:9091 {
        uri /api/verify?rd=https://auth.example.com
        # Authelia returns the authenticated user in Remote-User; copy_headers
        # writes it onto the upstream request, so clauster sees Authelia's value.
        copy_headers Remote-User Remote-Groups Remote-Email
    }
    reverse_proxy clauster:7621
}
```

```yaml
# clauster.yml
auth:
  enabled: true
  allowed_origins: ["https://clauster.example.com"]
  reverse_proxy:
    enabled: true
    require_hmac: false
    user_header: Remote-User
    trusted_ips: ["10.0.0.2"]   # Caddy's peer IP as seen by clauster
```

### Recipe — oauth2-proxy (header injection)

oauth2-proxy authenticates against your OIDC/OAuth provider and injects the user
into a configurable header. Point clauster's `user_header` at it.

```ini
# oauth2-proxy.cfg
upstreams = ["http://clauster:7621/"]
set_xauthrequest = true                 # emit X-Auth-Request-User upstream
pass_user_headers = true
# oauth2-proxy strips inbound X-Auth-Request-* from the client by default.
```

```yaml
# clauster.yml
auth:
  enabled: true
  allowed_origins: ["https://clauster.example.com"]
  reverse_proxy:
    enabled: true
    require_hmac: false
    user_header: X-Auth-Request-User
    trusted_ips: ["10.0.0.3"]   # oauth2-proxy's peer IP
```

Traefik (`forwardAuth` middleware) and authentik (outpost) follow the same shape:
authenticate at the proxy, forward an authenticated user header, list the proxy's
peer IP in `trusted_ips`, and ensure the proxy strips the client-supplied header.

## Scraping `/metrics` from behind the auth gate

The optional Prometheus `/metrics` endpoint
(`observability.prometheus_enabled`) sits **behind the same auth guard** as the
rest of the dashboard, so on a non-loopback, authenticated bind a scraper can't
reach it without credentials. Two ways through:

- **Scrape over loopback** — Prometheus on the same host scrapes
  `http://127.0.0.1:7621/metrics`, where no auth is enforced.
- **Set a scrape token** — `observability.metrics_token_hash` (mint with
  `clauster hash-metrics-token`) lets a scraper present `Authorization: Bearer
  <token>` to reach `/metrics` (only that route) without a browser session. This
  is the path for an off-host Prometheus.

The token journey, the full metric list, and a `prometheus.yml` snippet are in
[Operations → Metrics](operations.md#metrics).

## Docker

The container binds `0.0.0.0` internally, so it **requires enforced auth to
start** — the bundled `compose.yaml` sets `CLAUSTER_AUTH_ENABLED=true` and
`CLAUSTER_AUTH_PASSWORD_REQUIRED=true` and expects a
`CLAUSTER_AUTH_PASSWORD_HASH`. See [Installation](installation.md#docker).
