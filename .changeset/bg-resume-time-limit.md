---
default: patch
---

A background resume whose folder checks hang, for example on a network mount, now fails with a 504 after 20 seconds and never resumes the job later.
