---
default: minor
---

The `claude.node_from_nvm` setting now defaults to **on**: bridges resolve nvm's `default` node and put its `node`/`npx`/`npm` plus nvm-global CLIs (e.g. `agent-browser`) on the bridge `PATH` in every spawn context (not just `bash -c`). `clauster doctor` warns when an nvm toolchain won't reach spawned bridges; set the option false to opt out.
