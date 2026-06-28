---
default: patch
---
Fix Interactive (PTY) sessions failing to launch under the standalone binary — spawn the keeper via a frozen-binary subcommand instead of the `python -m` form that the binary's CLI rejects
