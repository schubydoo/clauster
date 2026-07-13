---
default: patch
---

`claude setup-token`'s authorize URL is now recovered from its OSC 8 hyperlink, so the dashboard login shepherd can read it under a Windows ConPTY where the terminal emits the URL as a hyperlink rather than plain text.
