---
default: security
---

A hosted session's stored conversation id is now shape-checked before it becomes the `--resume` value. A tampered record can no longer add an argument to the spawn command line. An ended session that cannot be resumed now says, on its row and in its panel, which part of its record is missing.
