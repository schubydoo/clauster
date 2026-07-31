---
default: patch
---

Fix the config editor offering a value it then refuses: a bounded field accepted an out-of-range value — including the endpoint of a bound that must be exceeded, not merely met — and only failed on save with a `422`.
