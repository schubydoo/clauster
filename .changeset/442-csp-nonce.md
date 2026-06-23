---
default: security
---
Gate inline scripts with a per-request CSP nonce and drop `'unsafe-inline'` from `script-src` (`'unsafe-eval'` and `style-src 'unsafe-inline'` remain, tracked as the #442 follow-up).
