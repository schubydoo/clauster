---
default: patch
---

Config panel fixes: clearing a JSON surface (Settings/Permissions/Hooks) and saving now treats a blank box as the empty default instead of rejecting it as invalid JSON, the typed scope confirm-token no longer lingers when you switch surface tabs, and the last inline `style` attributes were converted to classes so the panel no longer trips the `style-src` CSP.
