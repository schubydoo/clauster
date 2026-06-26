---
default: minor
---

Surface hard readiness blockers at the point of action: the per-project "Run Claude here" control now disables (with a reason tooltip and `aria-disabled`) when launching would be refused outright — a non-git directory with worktree as the spawn mode — instead of signalling only via the navbar/readiness pill, so the blocker is visible before the click rather than discovered by a failed launch.
