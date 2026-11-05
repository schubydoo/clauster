---
default: minor
---

The config editor accepts every subagent frontmatter value Claude Code reads and now refuses values it rejects or ignores, refuses an `mcpServers` map on write (the editor shows it converted to the list form, so a save fixes the file), and treats all six built-in agents as read-only.
