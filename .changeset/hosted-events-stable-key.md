---
default: patch
---

Fix a transient mis-render in the hosted live-event stream: key the events `x-for` on a stable per-item id instead of the list index, so the `MAX_LOG_LINES` front-splice no longer rebinds rows to the wrong events past 1000 lines.
