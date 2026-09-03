---
default: patch
---

A pyte render fault during the PTY keeper's connect-URL scrape no longer kills the keeper. Before, it stranded the discovery sidecar at `starting` or `ready` with the bridge orphaned. The scrape now skips that chunk and retries on the next one.
