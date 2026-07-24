---
default: patch
---

`clauster restore --config-out` now writes the restored `clauster.yml` with mode `0600` instead of a umask-derived `0644`. The config carries the argon2 `auth.password_hash` (and `api_token_hash`), so the old behaviour published it to every local user for offline cracking — and with `--force` it actively relaxed an existing `0600` file. The restored config is now owner-only, matching what `config_writer` and the setup wizard already maintain.
