---
default: patch
---

A Background session that is not registered on claude.ai now requires a first prompt — launched blank it would idle at "send a prompt to start" forever with no way to receive one; the launch popover marks the field required and the dispatch API rejects it with a 422.
