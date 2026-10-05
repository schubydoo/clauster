---
default: patch
---

The standalone binary now keeps its unpacked program files fresh, so an age-based `/tmp` cleanup no longer breaks the dashboard after about 10 days of uptime, and `/healthz` answers 503 when those files are missing.
