---
default: patch
---

The dashboard `claude setup-token` login now waits for the authorize URL to be stable across two polls before showing it, so a URL caught mid-render (split across the pty reader's chunks) is never surfaced truncated.
