---
default: patch
---

A self-referential YAML alias in subagent or skill frontmatter is now rejected as a 422 at write time. Before, the write was accepted and reading the stored value back failed with a 500.
