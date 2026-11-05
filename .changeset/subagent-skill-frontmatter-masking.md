---
default: minor
---

The Subagents editor now masks frontmatter secret values as `********` and keeps the stored value when a mask is saved unchanged, and the Skills editor now keeps a masked line on save instead of writing the mask over it; a mask that cannot be bound to its stored value is now refused.
