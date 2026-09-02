---
default: patch
---

A corrupt `state.json` or `hosted_state.json` now degrades to an empty store as documented — with a warning naming the failure and a one-time `.bak` of the file — instead of taking clauster down at startup when the file holds deeply-nested JSON or an over-long integer literal.
