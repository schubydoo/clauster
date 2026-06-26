---
default: minor
---

Add a read-only, paginated in-dashboard transcript viewer. A per-project "View transcript" button opens a modal that lists each session's on-disk `.jsonl` transcript (newest-first, with turn counts) and renders a selected session's turns — role, model, timestamp, and text — with cursor-based "Load more". Every turn's text passes through `redact.sanitize_line` before it reaches the browser (no session/env ids or secret shapes leak); reads run off the event loop, stream line-by-line, validate the project name and reject path-unsafe session ids, and fail closed to a 503 (never a 500 or a leaked path). Turn content renders via Alpine `x-text` only, never `x-html`. Closes the biggest history-review gap: reviewing what an agent did no longer needs SSH + `cat`.
