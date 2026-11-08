---
default: patch
---

The config editor now refuses frontmatter and Settings saves nested more than 64 levels deep with a named `422` before any slow parse, and a plugin or marketplace list nested that deep is a named error instead of a `500`.
