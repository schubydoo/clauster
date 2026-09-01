---
default: patch
---

Stop a YAML parse error from echoing the offending frontmatter token to the dashboard: a rejected subagent or skill now reports the error's category and line/column instead of PyYAML's message, which for an undefined alias, duplicate anchor or unknown tag quoted the token itself.
