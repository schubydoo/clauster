---
default: patch
---

Fix six defects across the CLI and write surfaces: a malformed `clauster.yml` exits 2 with one line instead of a traceback (and `doctor` diagnoses it), the Windows service argv accepts a quoted config path, subagent write and delete run their guards inside the file lock, a non-object credentials file raises `CredentialsError` rather than failing a spawn, `claustrum` daemon status carries a reason for every failure, and one unreadable skill no longer breaks the skills listing.
