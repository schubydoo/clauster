---
default: patch
---

Unify the launch + permission mode labels into one server-injected canonical map (`{mode: {short, long, effect}}`), so the launch picker, the inline JS helpers, and the config editor all read a single source instead of three hand-maintained copies (#685).
