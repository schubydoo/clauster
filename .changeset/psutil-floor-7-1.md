---
default: patch
---

Clauster now requires psutil 7.1 or newer: its process constructor no longer needs a readable boot time, so stopping a session on a host whose `/proc` reports none cannot raise mid-stop.
