---
default: security
---

A hosted session's stored conversation id is now checked for shape before it becomes the `--resume` value. A tampered record can no longer add an argument to the spawn command line. An ended session that cannot be resumed now says which part of its record is missing.
