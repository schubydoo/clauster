---
default: patch
---

Teach the claustrum client to reach the daemon over a Windows named pipe (discovered via the `rpc.pipe` file beside the socket) on Windows, where asyncio cannot consume the AF_UNIX socket; POSIX keeps using the AF_UNIX socket unchanged.
