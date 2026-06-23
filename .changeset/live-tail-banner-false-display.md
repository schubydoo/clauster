---
default: patch
---

Dashboard: the live-tail "reconnecting…" and "disconnected" banners no longer appear falsely while the log is streaming. A `d-flex` utility class (`display:flex !important`) was overriding the inline `display:none` that `x-show` uses to hide them, so both banners stayed pinned visible whenever the log panel was open — regardless of the tail's actual state. The flex layout now lives on an inner wrapper, leaving `x-show` free to hide the banner.
