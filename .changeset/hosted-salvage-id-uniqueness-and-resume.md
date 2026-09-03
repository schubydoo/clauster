---
default: patch
---

Two gaps a corrupt `hosted_state.json` could open are now closed. Two records that share an instance id no longer let a cached client id resolve to the wrong session. A session whose saved project is unreadable now says why it cannot be resumed, instead of offering a Resume that could never work.
