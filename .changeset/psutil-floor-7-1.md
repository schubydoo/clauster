---
default: patch
---

Clauster now requires psutil 7.1 or newer. Stopping a session on a host whose `/proc` reports no boot time no longer raises mid-stop, because the process constructor needs no clock there. The force-kill still takes the keeper down when its child tree cannot be read.
