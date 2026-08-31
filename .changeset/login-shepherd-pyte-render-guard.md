---
default: patch
---

A terminal frame that pyte cannot render no longer crashes the dashboard login mid-flow — the authorize-URL and token scans skip that frame, keep scanning, and name the fault if the sign-in still fails.
