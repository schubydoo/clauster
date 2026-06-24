---
default: patch
---

Hosted sessions: fix `ProcessStream` subscriber leaks. `HostedSession.start()` and `reattach()` now drop their stream subscription if the spawn/reattach RPC fails or times out (the error path previously left an undrained subscriber on the stream), and the pump loop drops its subscription whenever it exits on its own — a natural agent exit or a daemon-loss error — rather than leaving it until a later `stop()`/`detach()`.
