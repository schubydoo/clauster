---
default: patch
---

Drop `'unsafe-inline'` from the CSP `style-src` by nonce-gating the inline `<style>` blocks and lifting every inline `style=""` attribute into a CSS class (#533).
