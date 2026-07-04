---
default: patch
---

A Direct Session's `instance_id` now persists across a clauster restart (both the JSON and DB hosted-state backends), so a client-cached id keeps resolving instead of 404ing against a freshly re-minted one.
