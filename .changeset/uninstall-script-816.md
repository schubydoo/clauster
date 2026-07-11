---
default: minor
---

Add an `uninstall.sh` / `uninstall.ps1` counterpart to the install scripts: it auto-detects the install method (standalone binary or uv/pipx/pip/scoop), removes the binary/package, the service unit, and the state directory + config, with `--dry-run`, `--keep-config`/`--keep-data`, a confirmation prompt, and fail-closed path safety.
