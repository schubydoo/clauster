---
default: patch
---

Fix two phone-width layout breaks on the dashboard: the active-session action row (Live terminal / Transcript / Stop) now wraps instead of forcing the whole page to scroll sideways — so Stop is reachable on a phone — and the "Run Claude here" launch popover is pinned to the viewport below the `sm` breakpoint instead of hanging off the left edge. (#1159)
