---
default: patch
---

When the host clock is corrected, a Direct Session started after this release keeps its Kill and Resume buttons. Before, a clock correction reported a running session as lost. Sessions started before it are unchanged until their next spawn.
