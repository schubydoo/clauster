---
default: minor
---

Add an optional `tls` config block so Clauster can terminate HTTPS natively from an existing cert + key (uvicorn `ssl_certfile`/`ssl_keyfile`), validated fail-closed at load and at server start.
