---
default: patch
---

`clauster restore --config-out` now writes the restored `clauster.yml` atomically at mode `0600` instead of copying it at a umask-derived `0644`. The config carries the argon2 `auth.password_hash` (and `api_token_hash`), so the old behaviour published it to every local user for offline cracking — and with `--force` it actively relaxed an existing `0600` file. The write now goes through a `mkstemp` temp (created owner-only) and an atomic replace, so the hash is never briefly world-readable, a failed restore leaves no permissive file behind, and a symlink sitting at `--config-out` is replaced rather than written through to an unrelated file. The destination's parent directory is deliberately left alone, since `--config-out` is often a shared location.
