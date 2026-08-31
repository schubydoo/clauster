---
default: patch
---

Subagent and skill frontmatter now parse identically — one shared fence (a closing `---` may carry trailing whitespace on both surfaces) and one shared YAML load that rejects an unfitting explicit tag (`!!int`, `!!float`, `!!bool`, `!!timestamp`) as a 422 instead of a 500.
