---
default: patch
---

An abandoned dashboard sign-in no longer wedges every later attempt: the login panel rehydrates its in-progress flow after a page reload (so Cancel is reachable), and a flow left idle for 15 minutes is reclaimed by the next sign-in.
