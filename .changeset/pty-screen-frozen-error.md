---
default: patch
---
Make the live-terminal "pyte unavailable" error honest on the standalone binary — pyte is LGPL and not bundled, so point binary users at a `pip`/`uv` install with the `[pty]` extra instead of the dead-end `install clauster[pty]`; documented in installation/configuration and the in-app editor help
