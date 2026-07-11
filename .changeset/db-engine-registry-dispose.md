---
default: patch
---

Close short-lived database engines' SQLite connections deterministically instead of leaving them for the garbage collector, which emitted spurious `unclosed database` warnings on Python 3.13+.
