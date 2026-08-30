---
default: patch
---

OSC escape sequences (terminal title, hyperlink, clipboard) are stripped whole from the streamed bridge log and the `claude login` URL scan instead of leaving their payload as visible text.
