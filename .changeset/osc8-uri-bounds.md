---
default: patch
---

An unterminated OSC 8 (terminal hyperlink) escape no longer swallows the following line into its URI. Before, on the Windows ConPTY login path, that handed the operator a corrupted, unopenable `claude setup-token` authorize URL. On that path the hyperlink target is the only recoverable copy.
