---
default: patch
---

Forget now clears a dead background agent even when `claude rm` soft-fails: clauster drops the orphaned job record itself, gated on the worker being confirmed dead so a live worker is never force-forgotten.
