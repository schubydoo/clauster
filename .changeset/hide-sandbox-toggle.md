---
default: patch
---

The per-launch **sandbox toggle is disabled for this release** (#1037). Evidence from the pre-RC dogfood showed `--sandbox` reached the remote-control bridge but was never passed to the server-mode session worker that runs Bash, so the security-labeled control silently did nothing — a "fail closed visibly" violation. The launch popover no longer offers it, the `sandbox` API/CLI/MCP parameter is accepted-but-inert (coerced to `default`), and any persisted `on`/`off` on a stopped card resumes safely as `default`. It returns behind dependency preflight + platform gating in #1046.
