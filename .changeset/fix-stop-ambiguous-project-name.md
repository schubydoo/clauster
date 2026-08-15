---
default: patch
---

Fix `clauster stop <project>` silently stopping nothing when a project has multiple instances (#1150). A bare project name that matches more than one instance now refuses with an "ambiguous" error (listing the candidate instance ids) instead of silently resolving to the last-registered row, which could be a stopped one while a live bridge keeps running. Matches the existing ambiguous id-prefix behaviour (#1099).
