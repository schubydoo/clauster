---
default: patch
---

Reading a settings.json that holds a huge integer or a NaN/Infinity float literal now returns a 422, not a 500. The message names only the class of the offending value, never the value.
