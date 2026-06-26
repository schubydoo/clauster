---
default: patch
---

The per-bridge live-session list now appears from the **first** live session, not only when a standard bridge has two or more. The row's "Open in Claude" link is the bridge *connect* URL (it opens the bridge, where you pick or start a session), so a single working session previously had no direct deep link — now every live session under a standard bridge gets its own `claude.ai/code` jump. The toggle label is singular/plural-aware ("1 live session" vs "N live sessions").
