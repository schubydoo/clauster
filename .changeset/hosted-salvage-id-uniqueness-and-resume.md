---
default: patch
---

Close two gaps a corrupt `hosted_state.json` could open: two records sharing an instance id no longer let a cached client id resolve to the wrong session, and a session whose saved project is unreadable now says why it cannot be resumed instead of offering a Resume that could never work.
