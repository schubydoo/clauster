---
default: patch
---

A corrupt `state.json` or `hosted_state.json` now degrades to an empty store as documented, instead of taking clauster down at startup when the file holds deeply-nested JSON or an over-long integer literal.
