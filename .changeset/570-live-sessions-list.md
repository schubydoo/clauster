---
default: minor
---

List every live session under a standard `claude remote-control` bridge: an Active-session card for a multi-session bridge now expands to enumerate each working session with a short-UUID label, uptime, and a per-session deep link into the Claude web app — sourced from the existing `agents --json` reconcile join (no new poll) and served by `/api/sessions/tracked`. pty (single-session) bridges are unaffected; session ids render via Alpine `x-text` only and the deep-link host is validated so untrusted CLI output can never produce a broken or attacker-controlled link.
