---
default: patch
---

The config editor now refuses frontmatter `[...]`/`{...}` values and Settings saves nested more than 64 levels deep (`422`) before any slow parse, and a plugin or marketplace list nested that deep is a named `400` (it was a `500` past about 250 levels).
