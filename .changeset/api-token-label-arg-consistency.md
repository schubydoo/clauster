---
default: patch
---

`clauster api-token issue`/`rotate`/`revoke` now accept the token label either as a positional argument or with `--label` — previously the three verbs disagreed (`issue` required `--label` while `rotate`/`revoke` took a positional), so `api-token revoke --label X` errored.
