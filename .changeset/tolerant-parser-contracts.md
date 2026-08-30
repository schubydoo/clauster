---
default: patch
---

Seven untrusted-input parsers now skip or reject a deeply-nested or non-object payload instead of raising, and a junk `daemon_last_seq` in `hosted_state.json` no longer blocks startup.
