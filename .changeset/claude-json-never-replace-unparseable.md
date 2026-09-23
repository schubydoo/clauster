---
default: patch
---

A `~/.claude.json` that exists but does not parse is now left byte-identical and the write fails with the reason, instead of being replaced with only the keys Clauster sets.
