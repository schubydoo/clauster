---
default: patch
---

The Skills editor keeps a saved script's mode on Linux and macOS, refuses a directory, FIFO, device or unreadable skill file with a named `422` instead of a `500` or a hang, and caps a stored skill at 4096 entries; skill and subagent frontmatter refuses an unquoted `?` inside `{...}` or `[...]` that YAML 1.1 and Claude Code's YAML 1.2 read differently.
