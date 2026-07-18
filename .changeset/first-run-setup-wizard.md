---
default: minor
---

First-run setup wizard (#978): running `clauster` with no `clauster.yml` now serves a small, loopback-only setup page instead of exiting with an error. Enter your projects folder, bind address, and a dashboard password, and it writes a config with authentication enabled, then restarts onto it. The wizard binds `127.0.0.1:7621` by default (override with `CLAUSTER_SETUP_PORT`) and gates the submit to a loopback origin, so only a local operator can complete setup.
