---
default: minor
---

Add a gated `config-write` surface for `settings.json` `hooks` (project + user scope) behind the off-by-default fail-closed Foundation gate; the structural validator only checks shape (recognized event, string matcher, `type: "command"`, non-empty command, optional int timeout) and never resolves, parses, or runs a hook command (#690).
