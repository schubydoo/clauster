---
default: patch
---

The Skills editor keeps a saved script's mode, refuses a directory, FIFO, device or unreadable skill file with a named `422` instead of a `500` or a hang, caps a stored skill at 4096 entries, and refuses an unquoted `?` inside `{...}` or `[...]` frontmatter that YAML 1.1 and Claude Code's YAML 1.2 read differently.
