---
default: minor
---

New `clauster deps list/install/uninstall <extra>` side-installs the optional `pty`/`notify` extras into `<state_dir>/deps` for the standalone binary (which adds that directory to its import path at startup), behind an explicit unsigned-wheel confirmation.
