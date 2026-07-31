---
default: minor
---

Fix six defects: a malformed `clauster.yml` now exits 2 with one line instead of a traceback from every CLI verb (and `doctor` diagnoses it), the Windows service argv rejects a quoted config path like the `.bat` renderer already did, subagent write/delete run their guards inside the file lock, a non-object credentials file raises `CredentialsError` instead of failing a spawn, `claustrum` daemon status carries the reason for every failure, and one unreadable skill no longer fails the whole skills listing.
