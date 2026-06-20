---
default: security
---
Store the `/metrics` scrape token as a SHA-256 hash at rest (`observability.metrics_token_hash`), matching the API token; mint one with the new `clauster hash-metrics-token` command.
