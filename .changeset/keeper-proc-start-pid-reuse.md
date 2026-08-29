---
default: patch
---

Persist each Interactive Session keeper's start time so `forget` can tell that keeper from a different one that later reused its process id, instead of refusing to clear the record.
