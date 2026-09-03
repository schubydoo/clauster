---
default: patch
---

When a pywinpty liveness check faults during teardown on Windows, a `claude` login flow no longer stays stuck `active`. The four ConPTY `isalive()` calls now read a fault as dead, and the teardown always clears the flow.
