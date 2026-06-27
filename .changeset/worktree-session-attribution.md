---
default: patch
---

Fix the dashboard's per-bridge live-session count (the #570/#622 expander) never appearing for a `spawn_mode: worktree` bridge. `claude remote-control --spawn worktree` runs each session in a per-session git worktree under `<project>/.claude/worktrees/`, but session→bridge attribution joined only on an exact project-root cwd match, so a worktree session read as EXTERNAL instead of TRACKED and was excluded from the count (and from the bridge's tracked-session liveness). Attribution now also matches a worktree-spawn bridge's `.claude/worktrees` subtree by containment (most-specific root first, so a nested project's bridge wins), while same-dir/session bridges keep the exact-cwd join.
