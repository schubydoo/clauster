---
default: patch
---

Clauster now requires psutil 7.1 or newer, and stopping a session on a host whose `/proc` reports no boot time no longer raises mid-stop: the process constructor needs no clock there, and the force-kill still takes the keeper down when its child tree cannot be read.
