---
default: patch
---

A running bridge no longer flips to Stopped — and its card is no longer deleted — when the host's clock is corrected: bridge liveness now compares a boot-relative start time (immune to the NTP slew that moves psutil's create-time) alongside the existing one, and the phantom-prune no longer treats a bridge Clauster itself holds as evidence of an unmanaged one.
