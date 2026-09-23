---
default: patch
---

The terminal screen no longer loses the text after a control character in the same read, so a login or connect link no longer goes missing depending on how the output was split, and a digit such as `²` inside an escape sequence no longer turns the screen off.
