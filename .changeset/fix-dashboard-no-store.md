---
default: patch
---

Serve rendered HTML pages with `Cache-Control: no-store` so a cached or bfcached copy can't replay a stale CSP nonce or survive a deploy as an outdated render.
