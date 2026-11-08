---
default: patch
---

The config editor's line masks now mask the next lines of a secret value that goes on past its line, such as `env: {token:`, an unclosed quote or an anchor.
