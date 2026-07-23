---
default: patch
---

Config-editor validation failures now surface a per-field, plain-language message (e.g. "clone.allowed_private_cidrs: '…' does not appear to be an IPv4 or IPv6 network") instead of the raw pydantic dump with internal model names and error URLs.
