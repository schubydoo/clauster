---
default: patch
---

A terminal frame that pyte cannot render no longer crashes the dashboard login mid-flow. The authorize-URL and token scans skip that frame and keep scanning. If the sign-in still fails, the fault is named.
