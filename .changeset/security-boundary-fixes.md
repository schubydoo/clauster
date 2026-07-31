---
default: patch
---

Fix three boundary defects: a non-ASCII setup token now returns 403 instead of 500, secrets nested inside a secret-shaped key are masked, and listing subagents no longer reads a plugin symlink's target.
