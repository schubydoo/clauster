---
default: patch
---

A secret inside a YAML `!!omap`, `!!pairs` or `!!set` in subagent or skill frontmatter is now masked in the displayed frontmatter. A billion-laughs alias pyramid is now refused as a 422, where before it tied up a worker on every read.
