---
default: patch
---

Headless CLI/MCP writers now serialize spawn/stop against the running server on a per-project cross-process lock, and state saves merge onto the store's current contents — a bridge can no longer be double-launched and a forgotten record no longer resurrects (#949).
