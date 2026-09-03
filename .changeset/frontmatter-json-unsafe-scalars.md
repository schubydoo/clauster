---
default: patch
---

Subagent or skill frontmatter that parses but holds a value the JSON response cannot represent is now refused as a 422. The shapes are a non-finite float (`.nan`/`.inf`), a non-UTF-8 `!!binary`, and an integer too large to serialize. Each returned a 500 before.
