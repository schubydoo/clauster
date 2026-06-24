---
default: patch
---

Security (redaction): the `sk-…` token pattern in the log/stream redactor now includes `_` in its character class, matching the GitHub/GitLab/clauster token patterns. Anthropic `sk-ant-…` keys can contain underscores — without this, such a key would not fully redact and could leak into the bridge debug log or the live-tail WebSocket stream.
