---
default: patch
---

Two dashboard error-UX consistency fixes: the desktop-bridge **Stop** now asks for confirmation like every other destructive action (forget / stop-agent / stop-hosted / kill-hosted previously all confirmed, but the default session type's Stop did not), and **error** toasts now persist until dismissed instead of auto-vanishing after 4.5s — several failures surface only as a toast, and the toast stack already has a close button. Non-error toasts still auto-dismiss.
