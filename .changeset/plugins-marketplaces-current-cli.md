---
default: minor
---

The Plugins and Settings tabs now mask marketplace `headers`, `headersHelper` and URL credentials, and plugin MCP `env`/`headers`/`args`, on read; the Plugins tab also reads the `additionalMarketplaces` alias, shows each marketplace's location, and returns 404 for a marketplace that the chosen scope does not declare.
