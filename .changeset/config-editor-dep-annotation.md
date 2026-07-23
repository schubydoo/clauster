---
default: patch
---

The config editor now tells a feature switch what it needs (#1016 Part 2): a switch whose optional dependency is missing — Direct Sessions (the `claustrum` binary), notifications (`apprise`), the live-terminal view (`pyte`) — shows a "Requires … — run `clauster deps install …`" note and can't be turned on, so you learn what a feature needs at the moment you'd enable it instead of finding it silently dormant. Availability is resolved exactly the way the runtime resolves it (honoring a configured `claustrum.binary`), so a dependency that IS present never greys out its switch.
