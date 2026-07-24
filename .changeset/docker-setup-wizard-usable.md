---
default: patch
---

The first-run setup wizard now works in Docker (#1017): it writes `clauster.yml` to the persistent `/config` volume (`CLAUSTER_CONFIG`) instead of the container's ephemeral layer, and — with `CLAUSTER_SETUP_HOST=0.0.0.0` (baked into the image) — binds a reachable interface gated by a one-time token printed to the container log, so a published port can complete first-run setup without hand-writing config or an SSH tunnel.
