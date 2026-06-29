---
default: patch
---

Fix `clauster keepers`: read the carded-project set from the DB-backed store, not the flat `state.json` (renamed `*.imported` after the JSON→DB migration) — otherwise every live keeper was mislabeled an orphan and `--kill` could reap a carded, dashboard-managed keeper.
