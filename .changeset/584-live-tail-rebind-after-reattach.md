---
default: patch
---

Fix the live bridge-log tail going dead after a service restart + bridge reattach (the upgrade path). A reattached bridge was rebuilt without its `bridge_debug_log_path`, so `/ws/bridge-log/{instance_id}` closed every connection immediately — the tail flickered through a few reconnect attempts and then gave up with "Live tail disconnected", leaving the operator blind to a bridge that was actually alive. Both reattach paths now re-bind the tail to the log the bridge is still writing: a pty survivor derives it from its keeper sidecar's shared spawn-set stem, and a standard survivor recovers the newest debug log it wrote. The Reconnect button also resets the consecutive-failure counter on a manual retry so it can no longer be a no-op once auto-reconnect has capped out.
