---
default: patch
---

A background agent whose worker ended now shows Resume and Forget instead of Stop, also when its last state reads `blocked`, and Resume of an agent that was registered with claude.ai adds `--remote-control` and tracks the job id that `claude` prints.
