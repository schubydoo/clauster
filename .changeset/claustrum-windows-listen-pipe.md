---
default: patch
---

On Windows the claustrum daemon is now launched with `-listen-pipe`, so it opens the named-pipe listener the Windows client dials (via the `rpc.pipe` file beside the socket); POSIX keeps using the AF_UNIX socket unchanged.
