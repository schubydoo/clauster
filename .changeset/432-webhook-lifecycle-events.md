---
default: minor
---

Expand the outbound webhook taxonomy beyond the four bridge events with `bg-settled` (a `claude --bg` background job settled), `permission-needed` (a hosted session parked a tool-permission prompt — the "come look" signal), and `clone-done` (a project clone finished). Each carries an `event_type` discriminator, is redacted before egress, and **defaults OFF** — opt in per-key under `webhooks.events`.
