---
default: patch
---

Config and CLAUDE.md writes now share one cross-process lock held on a file in the state dir, so the editor and config-write paths mutually exclude across processes without leaving a `CLAUDE.md.lock` artifact in project directories.
