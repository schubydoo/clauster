---
default: patch
---

Reject a self-referential YAML alias in subagent/skill frontmatter at the parse seam (a 422) instead of accepting the write and then failing with a 500 when the stored value is read back.
