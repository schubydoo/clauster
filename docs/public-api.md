# Public API (`/api/v1`)

Clauster exposes a small, documented, versioned subset of its HTTP surface under
`/api/v1` so you can drive it from your own scripts and apps instead of the
dashboard's internal `/api/...` routes. This is **single-operator** scoped: every
token authenticates as the same operator identity the dashboard session does —
there is no per-user/multi-account auth (see [Security](security.md)).

## What's public vs internal

- **Public (`/api/v1/...`), documented and stable.** The resource endpoints:
  project list, session reads, instance spawn/stop/resume, agent spawn/stop/resume.
- **Internal (bare `/api/...` only), unversioned, may change without notice.**
  Everything else — HTML-fragment/partial routes the Alpine dashboard renders
  (`/api/projects/{name}/row`, `/api/widget`, and similar), config-write,
  environments, project create/clone, and the per-instance `message` /
  `permissions/{request_id}` / `forget` / `qr` routes.

`/api/v1/...` is an **alias**, not a fork: each `/api/v1` route is served by the
exact same handler as its bare `/api/...` counterpart, so the two always return
identical payloads for the same request. The dashboard keeps calling the bare
routes directly — this is purely additive; nothing about the internal surface
changed.

| Method | Path (bare `/api/...` and `/api/v1/...`) | What |
| --- | --- | --- |
| GET | `/projects` | List projects |
| GET | `/sessions` | External (unmanaged) working sessions, by project |
| GET | `/sessions/tracked` | Live sessions owned by a managed bridge, by instance |
| GET | `/sessions/adoptable` | Project names whose live external session is adoptable |
| GET | `/instances` | Every managed bridge/hosted instance |
| POST | `/instances` | Spawn a bridge/session |
| GET | `/instances/{instance_id}` | Read one instance |
| DELETE | `/instances/{instance_id}` | Stop an instance |
| POST | `/instances/{instance_id}/resume` | Resume a stopped/crashed instance |
| GET | `/agents` | List `claude --bg` background sessions |
| POST | `/agents` | Dispatch a background session |
| DELETE | `/agents/{job_id}` | Stop a background session |
| POST | `/agents/{job_id}/resume` | Resume an ended background session |

A request to a route not on this list (e.g. `GET /api/v1/widget`) 404s — the
internal surface was never aliased, by design.

## Authenticating

Every `/api/v1` request needs the same credential any other `/api/*` route
needs whenever `auth.enabled` is set: a session cookie, trusted-reverse-proxy
auth, or an `Authorization: Bearer <token>` header. A headless/API client uses a
Bearer token — see [Named API tokens](#named-api-tokens-clauster-api-token)
below to mint one. With `auth.enabled: false` the whole API (bare and `/api/v1`
alike) is unauthenticated, matching the existing `/api/*` posture.

```sh
curl -H "Authorization: Bearer clauster_pat_…" https://clauster.example/api/v1/instances
```

## Named API tokens (`clauster api-token`)

Tokens are managed **CLI-first** — there is no dashboard surface for token
management yet (planned for a later release). Each token has a unique,
operator-chosen `--label`; only its SHA-256 hash is ever stored, and a raw
token is shown to you exactly once, at issue/rotate time.

```sh
clauster api-token issue --label ci-runner    # mint + print a new token (once)
clauster api-token list                       # label, created, last-used — never the token
clauster api-token rotate ci-runner           # mint a fresh secret for an existing label
clauster api-token revoke ci-runner           # delete a token; it stops authenticating immediately
```

Tokens never expire by default and are revocable at any time. The legacy
single `auth.api_token_hash` config field (from `clauster hash-token`, #360)
keeps working unchanged alongside any named tokens — it's checked as one more
accepted credential, so upgrading doesn't invalidate an existing deployment's
token.

## OpenAPI docs (`/docs`, `/openapi.json`)

Off by default: `api.openapi_enabled: false` means the routes don't exist at
all (a 404), so an undocumented deployment never advertises its own shape to a
prober. Set `api.openapi_enabled: true` in `clauster.yml` (restart required) to
serve the interactive docs UI at `/docs` and the raw schema at `/openapi.json`.
Both sit **behind the same auth guard** as every other `/api/*` route — an
unauthenticated request gets a `401`, not a login-page redirect, and there is
no way to expose docs without also exposing the API they document. See the
`api` section in [Configuration](configuration.md) for the full toggle
reference.

## API-only deployment

Set `ui.enabled: false` (default `true`, restart required) to run Clauster as a
pure JSON API with no browser dashboard at all (#806):

```yaml
ui:
  enabled: false
```

With the UI off, these all 404: the `/` dashboard page, `/login` + `/logout`,
`/static/*`, and the internal HTML-fragment / per-session interactive routes
(`/api/projects/{name}/row`, `/api/widget`, and the per-instance `message` /
`permissions/{request_id}` / `forget` / `qr` routes) — the exact "internal,
unversioned" set described above. Everything else is unaffected: the rest of
the bare `/api/...` routes, `/api/v1/...`, `/healthz`, `/metrics` (per its own
gate), and the WebSocket streams all keep working, still behind whatever auth
is configured.

`ui.enabled` is **independent** of `api.openapi_enabled` and every `auth.*`
setting — none of them implies another. Every combination is valid: dashboard
and API together (the default), API-only, a UI with `/docs` off, and so on.

!!! warning "No login page means no session-cookie auth"
    With the UI off there is no `/login` route, so session-cookie (and
    password) auth is unreachable. Only a Bearer token — the legacy
    `auth.api_token_hash` or a named `clauster api-token` — or a trusted
    reverse proxy can still authenticate. If `auth.enabled` is on and neither
    is configured, Clauster logs a loud startup warning rather than refusing to
    start (refusing outright would brick a deployment that flips `ui.enabled`
    off before minting a token — a stricter fail-closed refusal is a
    reasonable alternative and open to revisiting). Mint a token first:
    `clauster api-token issue --label ci-runner`.
