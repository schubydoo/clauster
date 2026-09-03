---
default: patch
---

A hosted session whose saved conversation id is off-shape now keeps that id on disk, so an operator can repair it by hand. The dashboard hides Resume for such a session, whether the id was captured live or reloaded, instead of offering a Resume that only errors.
