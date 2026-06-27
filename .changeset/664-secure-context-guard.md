---
default: patch
---

The config editor now flags when browser notifications can't be delivered in your browser/connection (insecure non-HTTPS context, unsupported browser, or blocked permission) and disables the toggle when it can't work, instead of silently offering a setting that does nothing. Browser notifications require a secure context (HTTPS or localhost).
