---
default: patch
---
Fix a freshly-spawned bridge's auto-created session briefly reading as an EXTERNAL/unmanaged "phantom" row during the bridge's Starting window — reconcile now attributes a STARTING bridge's cwd, not only a live one (#713)
