---
default: patch
---

Fix workspace trust for Claude Code 2.1.232+: a nested git repository now requires its own trust grant (a parent grant no longer cascades), so the badge and spawn gate fail closed instead of showing green over a directory the CLI rejects, and a new dashboard "Trust all" action reconciles installs whose repos were trusted only via a parent. (#1224)
