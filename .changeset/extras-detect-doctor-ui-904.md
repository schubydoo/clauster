---
default: patch
---

`clauster doctor` now reports each optional extra (`pty`/`notify`) as OK or WARN with an install hint, and the dashboard's live-terminal control renders greyed with that hint when the tap is enabled but the `pyte` extra is missing instead of silently vanishing.
