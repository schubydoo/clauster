---
default: patch
---

Dashboard: unified the two pre-launch warning vocabularies (UX-07). Both the header
system-wide pill and the per-project pill now read as "readiness checks" — the header
tooltip says "System readiness … affects every launch on this host", the per-project
pill drops the "preflight" jargon for "N check(s)" with a "for this project before
launch" tooltip and a "Readiness checks for &lt;project&gt;" detail heading. One term,
scoped by wording; internal names, API routes, and test hooks are unchanged.
