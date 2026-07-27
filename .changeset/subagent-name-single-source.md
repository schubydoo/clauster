---
default: minor
---

The config panel's subagent editor now treats the **Name** box as the single source of the agent's name — it's written into the frontmatter `name:` automatically on save (the backend requires the two to match), so you no longer type it twice. New agents start from a small frontmatter template that omits `name:`.
