---
default: patch
---

Config editor: several correctness + UX papercuts. The usage badge mode (`usage.mode`) is now authoritative over the deprecated `usage.show_cost` alias — `show_cost` is flagged deprecated in the panel (with a plain-language note pointing at `usage.mode`) instead of leaking its raw docstring, and an explicit `usage.mode` wins if both are set. The transcript-recap toggle now greys out under the pty (true-resume) launch mode, where it has no effect. The bridge-log retention knobs (`logs.retention_max_age_days` / `_max_files` / `_max_total_mb`) are now editable in-app alongside the other log settings. And a blank `usage.currency_symbol` falls back to the default symbol instead of rendering an empty badge.
