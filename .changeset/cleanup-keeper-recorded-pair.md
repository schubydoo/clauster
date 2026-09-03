---
default: patch
---

Stopping an Interactive Session now checks the keeper PID against the keeper start time recorded in the instance row. It no longer trusts only a snapshot taken as the stop begins. A PID recycled onto an unrelated process before the stop ran is no longer force-killed.
