---
default: patch
---

Password managers no longer offer to fill non-credential dashboard fields (#1036). Every non-password `input`/`textarea` — config-editor rows, launch popovers, the clone URL, the first-run setup wizard — now carries the per-vendor opt-out attributes (`data-1p-ignore` / `data-lpignore` / `data-bwignore` / `data-form-type="other"`) alongside `autocomplete="off"`, which Chromium Autofill and the manager extensions ignore on its own (crbug 468153). Applied in the templates (a `{{ NO_AUTOFILL }}` global) so Alpine `x-for` row clones and the Alpine-free setup page inherit it. The login and setup **password** fields deliberately omit it, so your manager still fills/saves credentials.
