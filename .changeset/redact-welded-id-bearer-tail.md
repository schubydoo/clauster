---
default: security
---

Redaction now masks an identifier written directly after a masked token, on every surface, and the whole value of a `Bearer` header that holds a UUID or another token, on the streamed log and the on-disk mirror.
