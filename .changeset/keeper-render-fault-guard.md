---
default: patch
---

A pyte render fault on the PTY keeper's connect-URL scrape no longer kills the keeper and strands its discovery sidecar at `starting`/`ready` with the bridge orphaned — the scrape skips that chunk and retries on the next one.
