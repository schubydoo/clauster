---
default: patch
---

A blank `auth.password_hash` can no longer be logged in with. `verify_password` returned success for any *falsy* stored hash, so an empty-string hash (reachable via `password_hash: ""` or a present-but-empty `CLAUSTER_AUTH_PASSWORD_HASH`) made the source-visible dummy password a working credential for both `/login` and the `/api/reauth` step-up. Success now requires a real configured hash, and a blank or whitespace-only `password_hash` normalizes to `null` at config load — mirroring `api_token_hash` — so the value is refused at both layers. The constant-time dummy verify still runs, so no timing oracle is opened.
