---
default: minor
---

The **Advanced** config panel now edits list and map fields too (#978): `clone.allowed_schemes` and `clone.allowed_private_cidrs` get a rows editor (add/remove entries), and `webhooks.events` gets a checkbox per known lifecycle event (each shown at its correct default, saving only the events you change). These join the existing Advanced scalars behind the same `config_write` capability + step-up re-auth. Secret URL lists (`webhooks.urls`, `notifications.urls`) and auth trust lists stay file/CLI-only — their masked values can't round-trip a browser edit safely — and every write is still fail-closed, re-validated (bad CIDR / unknown event rejected), and audited by key-name only.
