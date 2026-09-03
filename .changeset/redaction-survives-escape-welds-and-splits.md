---
default: security
---

Log redaction now masks against the view a browser renders. An escape sequence or an invisible control character can no longer weld a session identifier onto the word before it, or split one in two. DCS, SOS, PM, and APC escape payloads (device-control and private-message strings) are now stripped whole, not streamed as readable junk.
