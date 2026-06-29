---
default: patch
---

Fix pty bridges intermittently showing "No web link — use Logs": the keeper now scrapes the connect URL from the pyte-reassembled screen (the live-view winsize makes claude fragment it with cursor-positioning escapes the raw scan can't follow), so "Open in Claude" surfaces reliably (#665).
