---
default: minor
---

The Settings tab now masks every credential helper, shows the merged view with Claude Code's own merge, and edits `disabledPlugins` and the settings-file MCP approval lists; it now refuses a key at a scope where Claude Code ignores it and a mask with no stored value behind it (such as a renamed masked `env` variable), and a failed load clears the previous scope's rows.
