---
default: patch
---

Fix the launch popover's consent gates squeezing their explanation text to about two words per line: Tabler's `.alert` is a flex row, so the trust, bypass-permissions and MCP-approval gates laid their explanation and controls out side by side until an explicit `display: block` restored stacking.
