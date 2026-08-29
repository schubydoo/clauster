---
default: patch
---

`clauster doctor` now reads the module version Go embeds in the claustrum binary when `--version` reports only the unstamped `claustrum-dev` sentinel, so the row names the installed release instead of shrugging "unstamped/dev or older build".
