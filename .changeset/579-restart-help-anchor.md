---
default: patch
---

Fix the config-editor "Saved — restart Clauster to apply" help link, which pointed at a nonexistent README `#running` anchor (a silent dead link). It now points at the published Operations runbook's restart section (`operations/#restart`), and that heading carries a stable custom anchor so the link can't rot from a future heading rename. A test pins the URL so a regression fails the suite instead of shipping another dead help link.
