---
default: minor
---

The Skills editor now reads every SKILL.md frontmatter field the way Claude Code reads it, accepts a skill with no frontmatter, and refuses values Claude Code rejects or ignores, hooks outside `hooks`, and YAML 1.1-only spellings in subagent and skill frontmatter (such as a nested `yes` or `1_000`), and it opens a file with ignored single-value fields converted, so a save fixes it.
