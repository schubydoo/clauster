---
default: patch
---

A background resume whose folder is gone answers 409 instead of 502, a bypass or inherit resume that cannot list projects_root gets a 403 that names the error, and Clauster no longer writes Infinity or NaN to `~/.claude.json` and refuses a file that holds them.
