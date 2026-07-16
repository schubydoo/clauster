---
default: patch
---

Internal cleanup from a slop-cleanup sweep: share the Anthropic HTTPS transport + client base between the environments and code-sessions clients, collapse the duplicate `~/.claude.json` atomic-writer onto the shared core, and tighten a few return/param types. No behavior change.
