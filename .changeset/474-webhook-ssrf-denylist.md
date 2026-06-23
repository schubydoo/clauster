---
default: minor
---

Webhooks gain an opt-in SSRF guard. Set `webhooks.block_private_targets: true` to skip any webhook URL whose host is an internal/non-routable IP literal — loopback, link-local (incl. the `169.254.169.254` cloud-metadata IP), RFC1918 private, unspecified (`0.0.0.0`/`::`), reserved, multicast, IPv6 ULA (`fc00::/7`), and carrier-grade NAT (`100.64/10`). It also catches the non-canonical IPv4 encodings the OS resolver still dials but `ipaddress` rejects (decimal-integer `2130706433`, hex, short `127.1`), and normalizes IPv4-mapped IPv6, so none of those slip past to loopback or the metadata endpoint. Defaults to **off**, so existing LAN/private receivers keep working unchanged. DNS hostnames are not resolved (rebinding) and exotic IPv6 embeddings (NAT64, IPv4-compatible) are not normalized — out of scope for this literal-IP seam.
