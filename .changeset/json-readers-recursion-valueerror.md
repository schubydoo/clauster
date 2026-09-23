---
default: patch
---

Deeply nested JSON and integers of more than 4300 digits now read as bad JSON instead of crashing the JSON readers that caught only malformed-JSON errors, the resume-recap hook installer no longer replaces a `~/.claude/settings.json` it cannot parse, and the legacy state import no longer renames a file it cannot read.
