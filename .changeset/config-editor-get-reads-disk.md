---
default: patch
---

Make the in-app config editor reflect the current on-disk config: `GET /api/config` now reads the editable field values from the file (consistent with the content hash) instead of the startup config captured in memory. A save writes the file but deliberately does not live-reload the running config, so previously reopening the editor after a save showed the stale pre-save values until a restart — making a successful save look reverted. The runtime still only adopts the change on restart (the `restart_required` flag is unchanged); an unreadable/corrupt file falls back to the in-memory values so the editor still opens.
