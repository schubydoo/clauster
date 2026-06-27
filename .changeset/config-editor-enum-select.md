---
default: patch
---

Fix the in-app config editor's enum dropdowns (Launch mode, Usage badge mode, and every other `<select>`) showing the first option instead of the saved value — `x-model` ran before its `x-for` options existed, so the browser fell back to option index 0 (e.g. displaying "Standard" while the bridge default was `pty`, or "Cost" while the badge was `off`), which also left Save greyed when you re-picked the real value; each option now binds `:selected` to the model value so the dropdown reflects what is actually on disk.
