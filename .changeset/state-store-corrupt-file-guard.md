---
default: patch
---

A corrupt legacy `state.json` / `hosted_state.json` now degrades to an empty store as documented, instead of taking clauster down on the boot that imports it. Every corrupt shape is logged and copied aside once as `*.corrupt.bak`, and `clauster migrate` refuses to rewrite a file it could not read.
