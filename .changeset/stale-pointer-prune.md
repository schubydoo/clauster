---
default: patch
---

On startup, clauster now garbage-collects its own stale `bridge-pointer.json` files — only pointers under `projects_root` that are both non-live and older than a 14-day retention window — so long-dead pointers stop accumulating (#671 hygiene). A live bridge's pointer, or a recently-stopped session's (still resumable), is never touched, and any listing/stat/delete error is logged, never fatal to startup.
