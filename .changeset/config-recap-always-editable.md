---
default: patch
---

Config editor: stop locking "Recap prior transcript on restart" (and its "Recap size limit" child) to the default launch mode. Launch mode is chosen per-spawn, so gating the toggle on the config's *default* launch mode was wrong — it greyed the option out whenever the default wasn't `standard`, and even rendered stale-disabled on first load. The toggle is now always editable, with an informational note that recap applies to standard bridges only (pty bridges resume natively via --continue).
