---
default: patch
---

The `.bak` sibling that a local-scope settings write leaves beside `.claude/settings.local.json` is now gitignored too. Only the exact target path was ignored, so the backup — a plaintext copy of the *previous* `env` map, this surface's one secret-bearing shape — sat un-ignored next to it and could be swept into a commit by `git add -A` and pushed. `ensure_gitignored` gained an opt-in `ignore_backup_sibling` flag, set by all four writers of that file (settings, hooks, skill overrides, permissions).
