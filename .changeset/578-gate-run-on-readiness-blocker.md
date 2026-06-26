---
default: minor
---

Surface a launch precondition inside the "Run Claude here" popover instead of in the navbar/readiness pill: when a non-git project's default spawn mode is `worktree` (which the server refuses, since worktree needs a git repo), opening the popover now falls the spawn picker back to `same-dir` and shows a note explaining it. Run stays enabled — it isn't disabled — so the spawn picker and trust-on-start confirm that resolve such preconditions remain reachable.
