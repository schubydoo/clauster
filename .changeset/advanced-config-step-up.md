---
default: minor
---

New **Advanced** config surface (#978): the operational-but-sensitive `clone.*` and `webhooks.*` scalars are now editable in-app behind the `config_write` capability **and** a step-up re-auth — `POST /api/reauth` re-proves the operator password for a short unlock window, and `GET`/`PUT /api/config/advanced` gate on that fresh proof. Lockout/exposure/RCE keys (bind, auth switches, `ui.enabled`, TLS, binaries, `config_write.*`, `login_shepherd.*`) stay file/CLI-only, and every Advanced write is fail-closed (backup + atomic replace) and audited by key-name only.
