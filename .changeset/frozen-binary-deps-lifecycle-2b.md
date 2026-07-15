---
default: minor
---

The standalone binary now bundles `pip`, so `clauster deps install <extra>` runs on it directly (no separate Python needed); `clauster deps install shawl` provides the pinned, checksum-verified [Shawl](https://github.com/mtkennerly/shawl) service wrapper that `install-service windows` now uses instead of nssm; and the uninstaller enumerates the side-installed extras/Shawl and can preserve them with `--keep-deps`.
