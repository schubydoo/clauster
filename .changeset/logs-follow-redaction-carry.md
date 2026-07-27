---
default: patch
---

`clauster logs` now withholds a half-flushed line until it completes, so redaction can never miss a secret split across two reads.
