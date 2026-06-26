---
default: minor
---

Add a sort-direction toggle and in-message search to the read-only transcript viewer. A header toggle flips a session between newest-first and oldest-first (an `order=asc|desc` query param, default `desc`, applied before the slice math so pagination still terminates), and a search box filters to turns containing a substring via a backend `q=` scan over the whole transcript — matched against the already-redacted content so a search can never confirm a masked secret, with clear "no matches" and "type at least 2 characters" states. Both reuse the path-safe `resolve_session_transcript` resolver, keep `redact.sanitize_line` on every field, stay off the event loop, and fail closed to 503; results still render via Alpine `x-text`, never `x-html`.
