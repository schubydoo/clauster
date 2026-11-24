---
default: patch
---

A resumed bridge's log stream can learn new ids again (each run has its own cap; earlier ids stay masked), a crafted bridge-log line no longer makes the marker parse quadratic, and the pty-screen view masks each frame once instead of on every poll.
