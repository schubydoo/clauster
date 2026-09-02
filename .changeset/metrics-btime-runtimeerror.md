---
default: patch
---

The dashboard metrics sampler now handles a host with no procfs `btime` line. It reports the bridge's own CPU and memory instead of failing the request.
