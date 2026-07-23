---
default: patch
---

The Transcripts selector (and the resume/fork picker that shares its endpoint) now opens near-instantly on unchanged transcripts (#1035): each file's `turn_count` plus the picker's label/timestamp fields are cached on a `(mtime, size)` stamp instead of re-parsing every `.jsonl` on every open — which stalled ~20 s on a project with a large transcript. Any append/change re-derives, so the listing stays correct.
