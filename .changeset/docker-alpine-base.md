---
default: minor
---

The Docker image now uses an Alpine (musl) base with explicitly pinned, Renovate-tracked apk packages instead of Debian slim with an unpinned `apt upgrade`; it is smaller and drops the Go-based `gosu` (now `su-exec`). Derived images must use `apk` rather than `apt`.
