---
default: patch
---

Surface the config-file search order in `clauster --help`: the epilog now lists `$CLAUSTER_CONFIG` → `./clauster.yml` → `$CLAUSTER_HOME/clauster.yml`, so the resolution order is discoverable from the CLI without digging through the docs.
