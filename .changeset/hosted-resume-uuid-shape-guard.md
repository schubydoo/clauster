---
default: security
---

A hosted session's stored conversation id is now checked for shape before it becomes the `--resume` value, so a tampered record cannot add an argument to the spawn command line, and an ended session that cannot be resumed now says on the row and in its panel which part of its record is missing.
