---
default: patch
---

Fix the live-transcript view on a running session: the live tail is now the single source (the static paged list is hidden while live) so turns no longer render twice or fall behind, the live feed honors the sort toggle instead of being hardcoded oldest-first, the toggle's label and aria-label both describe the same sort state, and both turn lists key on a stable id rather than the array index.
