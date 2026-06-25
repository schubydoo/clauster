---
default: patch
---

a11y: the clone-progress bar now exposes ARIA value attributes so assistive tech can announce progress. It carries `aria-valuemin="0"` / `aria-valuemax="100"` always, `aria-valuenow` bound to the percent during the determinate phase, and `aria-valuetext` (the current phase label) during the indeterminate phase — previously it had `role="progressbar"` with no value, so screen readers announced nothing.
