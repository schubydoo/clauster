---
default: security
---

Redact a failed clone's `error_detail` (a tail of git stderr) on the progress WebSocket, closing a redaction asymmetry: the same value was already redacted on the clone-done webhook path, but went out raw over the WS. The redaction now happens inside `terminal_event()`, so every consumer of that frame — the live broadcast and the reconnect snapshot — is covered. Defense-in-depth only (the WS is auth-gated to the operator who typed the clone URL, and git scrubs inline credentials from its own stderr); the point is symmetry across both egress paths.
