---
default: patch
---

The config panel no longer rejects valid Skills and Subagents that carry forward-compatible frontmatter keys (e.g. `effort`, `license`, `metadata`), and no longer masks a benign `author` field as a secret.
