---
default: patch
---

The auth and web-UI guards now strip the configured `root_path` before matching routes, so path classification stays correct behind a reverse proxy that does not strip the mount prefix (defense-in-depth; the supported prefix-stripping proxy is unaffected).
