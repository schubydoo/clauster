---
default: patch
---

Fix dashboard elements that could stay visible after a rapid hide→show→hide (an Alpine.js `x-show` cascade race under load) — the root of the e2e "Manage button never retires" flake.
