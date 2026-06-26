---
default: patch
---

Correct the `webhooks.block_private_targets` config-field description: it claimed DNS hostnames are not resolved, but the opt-in SSRF guard does resolve them at filter time — documentation only, behaviour unchanged.
