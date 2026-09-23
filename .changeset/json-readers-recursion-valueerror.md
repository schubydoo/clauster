---
default: patch
---

Deeply nested JSON and integers of more than 4300 digits now read as bad JSON instead of crashing the JSON readers in 13 modules, and the legacy state import no longer renames a file it could not read.
