---
default: patch
---

In the standalone binary on Linux and macOS, a newly started pty keeper runs as its own copy of the binary, so it keeps working after a Clauster restart deletes the server's unpacked files.
