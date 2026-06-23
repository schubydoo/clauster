---
default: minor
---

The Projects zone gains an optional sort control (Name / Last used / Cost). It defaults to the existing A–Z order and never reorders on its own — only when you pick a non-name sort does the list reorder (most-recent or highest-cost first, projects with no recorded history sinking to the bottom) and reveal every project. A new read-only `/api/projects/sortmeta` endpoint supplies the last-used and cost keys from the session-history rollup; the sort itself happens client-side and degrades silently to name order if the data can't be read.
