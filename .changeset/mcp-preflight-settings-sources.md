---
default: patch
---

The unapproved-MCP-server launch preflight now also treats a server decided (enabled or disabled) in a project's `.claude/settings.json` / `settings.local.json` or the user `~/.claude/settings.json` as decided — matching which sources claude actually honors — so it no longer falsely warns about a server that shows no enable gate.
