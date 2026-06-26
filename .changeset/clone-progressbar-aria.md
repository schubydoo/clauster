---
default: patch
---

a11y: the clone-progress bar now exposes `aria-valuemin`, `aria-valuemax`, `aria-valuenow`, and `aria-valuetext` so assistive technology can announce progress (previously `role="progressbar"` had no value attributes).
