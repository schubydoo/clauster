---
default: patch
---

A corrupt legacy `state.json` / `hosted_state.json` now degrades to an empty store as documented, instead of taking clauster down on the boot that imports it; that import logs every corrupt shape and copies the file aside once as `*.corrupt.bak`, while `clauster migrate` refuses to rewrite a file it could not read and leaves it untouched.
