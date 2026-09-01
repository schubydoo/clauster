---
default: patch
---

Honour the documented "malformed pointer → None" contract for a hostile `bridge-pointer.json`: deeply-nested JSON and an oversized integer literal now degrade to None instead of raising out of pointer loading. One caller degrades further — `clauster` can now back up and remove such a pointer (as it already did for other corrupt ones) rather than leaving it to wedge the next bridge start.
