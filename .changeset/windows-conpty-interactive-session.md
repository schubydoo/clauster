---
default: patch
---

Interactive Session (true-resume PTY) now runs on Windows: the keeper drives the bridge over a ConPTY pseudo-console via pywinpty (`pip install 'clauster[pty]'`), falling back to Server Mode when the extra is absent.
