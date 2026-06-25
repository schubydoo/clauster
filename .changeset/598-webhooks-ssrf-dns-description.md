---
default: patch
---

Correct the `webhooks.block_private_targets` config-field description (and the generated config reference it produces): it claimed DNS hostnames are not resolved, but the opt-in webhook SSRF guard does resolve them at filter time — a hostname pointing straight at a private/loopback/metadata IP is dropped, while a rebinding domain that re-resolves at dial time remains an acknowledged TOCTOU residual. Documentation only; the guard's behaviour is unchanged.
