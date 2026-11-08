---
default: patch
---

The Skills editor masks `mcpServers` `headers` and `env` values and hook `headers` values in `SKILL.md` frontmatter, also any server or hook value that those tabs mask by its loaded value (an escaped credential URL or `--api-key=` item in `args`), and its line masks now catch list items, flow-mapping pairs and block values under a secret-shaped key.
