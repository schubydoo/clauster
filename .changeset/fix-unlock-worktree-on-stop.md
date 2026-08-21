---
default: patch
---

Stopping an Interactive Session (pty, `spawn_mode: worktree`) now releases the git lock Claude Code placed on the session's worktree, so `git worktree remove` no longer refuses it with a dead-pid lock reason. The worktree and its branch are still left in place (they may hold uncommitted work, and a resume reuses them); only the now-pointless lock is dropped. (#1089)
