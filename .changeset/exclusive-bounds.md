---
default: patch
---

Fix the config editor offering a value it then refuses: a field with an exclusive bound (a timeout that must be greater than zero) advertised the endpoint as valid and only failed on save with a `422`.
