---
default: patch
---

Dashboard polish: the UI no longer shows raw enum tokens where a friendly label exists. The **Run** button and the hosted-session row now read "Auto-accept edits" / "Never prompt" etc. instead of `acceptEdits` / `dontAsk`; the Active-zone **filter chips** use the launch-menu product names (Desktop / Browser / Fire-and-forget) instead of internal tokens; and the config editor's **launch-mode, spawn-mode, and usage-mode** dropdowns get the same friendly labels as permission mode (the saved value is unchanged). Also fixes two accessibility gaps: the "First prompt" input and the New-project "Type" radio group are now properly associated with their labels for screen readers.
