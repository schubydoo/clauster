---
default: patch
---

The dashboard's long-lived `setup-token` login now runs on Windows over a ConPTY (`pip install 'clauster[pty]'`), with the operator-pasted code redacted from returned output since a ConPTY echoes input back.
