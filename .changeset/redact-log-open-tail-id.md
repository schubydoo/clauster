---
default: security
---

The streamed log and the on-disk mirror now mask a real `01`-shape session or environment id that has a word written directly after it (`session_01<id>_backup`), and every id in a chain of ids written with no separator.
