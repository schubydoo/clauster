---
default: patch
---

A hostile `bridge-pointer.json` now follows the documented contract that a malformed pointer degrades to None. Deeply-nested JSON and an oversized integer literal no longer raise out of pointer loading. `clauster` can now back up and remove such a pointer, as it already did for other corrupt ones, instead of leaving it to wedge the next bridge start.
