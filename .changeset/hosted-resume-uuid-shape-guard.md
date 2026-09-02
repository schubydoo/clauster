---
default: security
---

A hosted session's stored conversation id is now checked for shape before it becomes the `--resume` value, so a tampered record cannot add an argument to the spawn command line.
