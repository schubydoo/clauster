---
default: minor
---

The Plugins tab now masks marketplace `headers`, `headersHelper` and URL credentials and plugin MCP `env`/`headers` on read, reads the `additionalMarketplaces` alias, shows each marketplace's location, and returns 404 for a marketplace that the chosen scope does not declare.
