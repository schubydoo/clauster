---
default: patch
---

A hosted session whose persisted record holds a wrong-typed value now reattaches with default metadata and a logged warning, instead of failing clauster's startup for every hosted session.
