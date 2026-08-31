---
default: patch
---

DCS/SOS/PM/APC escape payloads are now stripped whole wherever Clauster strips ANSI, stripping a sequence can no longer weld two words together and hide an identifier or secret from the masks that follow it, a stray OSC 8 opener no longer swallows the real hyperlink, and the live pty screen re-redacts each row after the width re-fit so truncation cannot shear an identifier into view.
