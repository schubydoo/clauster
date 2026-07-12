---
default: patch
---

On Windows the claustrum daemon is now spawned with `-token-file` instead of `-token-fd 0` (a numeric fd is not a usable token handle for the Go daemon there), so clauster can start and drive a hosted-channel daemon over the named pipe.
