---
default: patch
---

A corrupt legacy `state.json` / `hosted_state.json` now degrades to an empty store as documented, instead of taking clauster down on the boot that imports it — deeply-nested JSON and an over-long integer literal used to escape the handler. Every corrupt shape is now logged and copied aside once as `*.corrupt.bak`, so the file stays recoverable.
