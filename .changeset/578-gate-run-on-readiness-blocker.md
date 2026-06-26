---
default: minor
---

When a non-git project's default spawn mode is `worktree`, the "Run Claude here" popover now falls back to `same-dir` and shows a note rather than letting the server reject the spawn, keeping the picker and trust-on-start confirm reachable.
