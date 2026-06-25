---
default: patch
---

README onboarding: align the "First bridge in 60 seconds" walkthrough with the shipped UI. Step 4 now says "Run Claude here" (the real launch control, not the removed "Start" button), the attach link is "Open in Claude" (not "Open session in Claude"), and the dead "Start new session" step is replaced with the actual fresh-start path (Forget the stopped session, then relaunch). Also removes the orphaned startNew/confirmNew/cancelNew dashboard handlers — the button was dropped in the #248 two-zone redesign and a test already guards against re-adding it — and the obsolete E2E-checklist step.
