---
default: security
---

A YAML `<<` merge-key pyramid in subagent or skill frontmatter is now refused as a 422. Before, it tied up a worker thread for seconds inside the parse on every read that lists agents or skills.
