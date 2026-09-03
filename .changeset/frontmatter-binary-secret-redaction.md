---
default: patch
---

A subagent's frontmatter now masks a secret written as a `!!binary` value. It masks the base64 spelling like the plain-string spelling, by key name and by secret-shaped content.
