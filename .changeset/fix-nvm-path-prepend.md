---
default: patch
---

`claude.node_from_nvm` now **prepends** nvm's resolved `default` node bin dir to the bridge `PATH` instead of appending it, so nvm's node wins over a distro `node` already on the inherited service PATH (e.g. `/usr/bin/node`). Appending let an earlier base node shadow nvm's, reintroducing the exact `npx`/`node` resolution failure the knob exists to fix — with the wrong node. Operator `claude.path_append` entries still append after the base. (#1018)
