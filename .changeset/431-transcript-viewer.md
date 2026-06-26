---
default: minor
---

Add a read-only in-dashboard transcript viewer: a per-project "View transcript" button opens a modal listing each session's `.jsonl` file (newest-first, with turn counts) and renders turns with cursor-based pagination; content passes through `redact.sanitize_line` and renders via Alpine `x-text` only.
