---
default: patch
---

Harden `atomic_write_text`: close the raw `mkstemp` fd if `os.fdopen` fails (an EMFILE/ENFILE leak the temp-only cleanup missed), and cover the write/fsync-failure and interrupt-mid-write cleanup paths with tests.
