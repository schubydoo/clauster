---
default: security
---

A YAML parse error no longer echoes the offending frontmatter token to the dashboard. A rejected subagent or skill now reports the error category with its line and column. Before, PyYAML's message quoted the token itself for an undefined alias, a duplicate anchor, or an unknown tag.
