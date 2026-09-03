---
default: patch
---

A config file that exists but cannot be read now fails the doctor config row plainly. The other CLI verbs now exit 2 with a `config error` message instead of a traceback. Before, an unreadable file made `clauster doctor` and `/api/doctor` answer a 500.
