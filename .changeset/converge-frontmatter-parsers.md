---
default: patch
---

Subagent and skill frontmatter now parse the same way. A `---` fence line may carry trailing whitespace on both surfaces. Before, a skill's opening fence with trailing whitespace was rejected. A skill body no longer keeps the whitespace after its closing fence. Both surfaces now reject a mismatched explicit tag (`!!int`, `!!float`, `!!bool`, `!!timestamp`) as a 422 instead of a 500.
