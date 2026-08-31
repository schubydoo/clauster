---
default: patch
---

Subagent and skill frontmatter now parse identically — one shared fence (a `---` line may carry trailing whitespace on both surfaces, where a skill's opening fence used to be rejected, and a skill's body no longer keeps the whitespace that followed its closing fence) and one shared YAML load that rejects an unfitting explicit tag (`!!int`, `!!float`, `!!bool`, `!!timestamp`) as a 422 instead of a 500.
