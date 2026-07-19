---
default: minor
---

`clauster deps install claustrum` now side-installs the **Direct Session daemon** — the standalone `claustrum` binary the hosted live-view channel needs. It downloads the pinned, SHA-256-verified release for your OS/arch (Linux/macOS/Windows × x86_64/arm64) from `schubydoo/claustrum` into `<state_dir>/deps/bin`, and Clauster's daemon launcher uses it automatically (an explicit `claustrum.binary` or a `claustrum` on `PATH` still wins). Fail-closed like the other managed binaries — a checksum mismatch refuses — and surfaced in `clauster deps list` / `doctor` (the doctor line only appears when `claustrum.enabled`). The pin auto-bumps via Renovate.
