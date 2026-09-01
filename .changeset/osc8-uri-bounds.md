---
default: patch
---

Stop an unterminated OSC 8 hyperlink from swallowing the following line into its URI, which could hand the operator a corrupted (unopenable) `claude setup-token` authorize URL on the Windows ConPTY login path, where the hyperlink target is the only recoverable copy.
