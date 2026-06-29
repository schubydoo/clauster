---
default: patch
---

Collapse the `/api/projects/sortmeta` N+1: a batched `sortmeta_for_all` (two grouped queries in one session) replaces the per-project 3-SELECT rollup loop on the dashboard sort path.
