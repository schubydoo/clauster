---
default: patch
---

Dashboard polish (#433): the project name now truncates at a **viewport-relative** width (≈10rem on a phone up to 28rem on a wide screen) instead of a fixed 16rem cap, so long names adapt to the screen (DES-07). The Active-list "Open in Claude" link now validates that the session URL is `http(s)` before binding it to the `href`, so a non-http value can never reach the link (FE-03 hardening).
