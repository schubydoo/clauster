---
default: patch
---

Fix `clauster install-service` so a frozen/standalone binary (or `clauster` console-script) install no longer emits a service unit with an invalid `clauster -m clauster run` command — the unit now invokes the clauster entry point directly across systemd, launchd, and Windows/nssm, and only a bare `python -m clauster` interpreter keeps the module prefix.
