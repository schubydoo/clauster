---
default: security
---

A secret inside a YAML `!!omap`, `!!pairs`, or `!!set` in subagent or skill frontmatter is now masked in the displayed frontmatter. An alias pyramid that expands exponentially (a billion-laughs attack) is now refused as a 422. Before, it tied up a worker on every read.
