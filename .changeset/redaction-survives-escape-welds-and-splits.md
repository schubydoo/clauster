---
default: patch
---

Log redaction now masks against the view a browser renders. An escape sequence or an invisible control character can no longer weld a session identifier onto the word before it. It can no longer split one in two either. DCS/SOS/PM/APC payloads are now stripped whole, not streamed as readable junk.
