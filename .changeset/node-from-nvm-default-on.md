---
default: minor
---

Bridges now resolve nvm's `default` node and put its `node`/`npx`/`npm` plus nvm-global CLIs (e.g. `agent-browser`) on the bridge `PATH` by default, so they work in every spawn context (not just `bash -c`); `clauster doctor` now warns when an nvm toolchain won't reach spawned bridges.
