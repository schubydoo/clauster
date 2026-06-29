---
default: patch
---

Stamp the per-request CSP nonce on xterm.js's runtime-injected `<style>` elements so the read-only live terminal renders under the strict nonce-gated `style-src` (no `unsafe-inline`).
