---
default: patch
---

Cross-OS robustness: reject Windows reserved device names (`CON`, `NUL`, …) as project names, honestly report a Windows background-agent stop (a hard kill, not a false clean stop), don't mislabel a transient clone-finalize failure as "already exists", preflight the AF_UNIX socket-path length for macOS, and warn when `nssm` is absent for the Windows service install.
