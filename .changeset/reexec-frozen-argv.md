---
default: patch
---

The in-app restart (`POST /api/restart`) and the first-run setup wizard's post-completion restart now work on the frozen binary (#1014): `_reexec` no longer passes the binary path twice in argv (a PyInstaller one-file build has `sys.executable == sys.argv[0]`), which previously aborted with `unrecognized arguments` and left the server dead — masked only where a service manager restarts on exit.
