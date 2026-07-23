---
default: patch
---

`clauster doctor` and the dashboard preflight panel no longer nag about the optional `apprise` dependency when notifications aren't actually going to send (#1016): the `extra:apprise` row now appears only when `notifications.enabled` is set **and** at least one `notifications.urls` entry is configured — matching when runtime actually imports apprise. (`pyte`/`pywinpty` stay ungated: pyte also reassembles the bridge connect-URL and pywinpty is the Windows Interactive-Session backend, so they matter beyond the opt-in live-terminal view. Binary deps like claustrum were already gated on their feature switch.)
