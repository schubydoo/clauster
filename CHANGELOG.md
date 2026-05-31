# Changelog

## [0.2.0](https://github.com/schubydoo/clauster/compare/v0.1.0...v0.2.0) (2026-05-31)


### Features

* auth foundation — password login, WS auth, reverse-proxy trust, state.json (v0.2) ([b9f40eb](https://github.com/schubydoo/clauster/commit/b9f40eb4081bfd28e0c4eeaf7750840db213e0ae))
* CLAUDE.md viewer/editor (v0.2, spec §5) ([4bb7a6e](https://github.com/schubydoo/clauster/commit/4bb7a6e23e0da8aed70ec36d56f2ebf3513cc7a8))
* cost / token tracking from session transcripts (v0.3) ([842e6dc](https://github.com/schubydoo/clauster/commit/842e6dc831331353a88358161ed598ef976fe51f))
* ghost-environment reaper dashboard UI, opt-in (v0.3) ([15d50e5](https://github.com/schubydoo/clauster/commit/15d50e5a75a0baf1f15be938b61859f1825aa5cf))
* ghost-environment reaper, dry-run default (v0.3, spec §11) ([5a5fadd](https://github.com/schubydoo/clauster/commit/5a5fadd496d9d5d186394cd18e6fdd568f9aa081))
* packaging/ops CLIs — doctor/backup/restore/migrate/install-service + PyInstaller (v0.2) ([b13f5e9](https://github.com/schubydoo/clauster/commit/b13f5e99d8861b253316a9fa7ac5bc49b5f0f36f))
* per-project cost badge on the dashboard (v0.3) ([7d67f94](https://github.com/schubydoo/clauster/commit/7d67f94ef86fe9d9375004e007e7f46869349c16))
* project create + clone (v0.2, spec §5 + §11 clone+trust chain) ([599c57b](https://github.com/schubydoo/clauster/commit/599c57b2127fc3e00a8beb8b2da256dcace09cd5))
* project discovery + dashboard scaffolding (v0.1 feature 1) ([54591cc](https://github.com/schubydoo/clauster/commit/54591cc8c4ea846e35b321787f61bac832d0d433))
* real logout revocation via server-held session epoch (v0.3) ([d0c37a5](https://github.com/schubydoo/clauster/commit/d0c37a5bfbe8be4c555b8ba6999a9c27f1600c86))
* SessionRunner — spawn/stop bridges + agents --json cross-check (v0.1 features 2-4) ([71a5965](https://github.com/schubydoo/clauster/commit/71a5965e8c1e11b24dcbc36d6f4fb8a9632a67b2))
* spawn-mode + permission-mode pickers, footgun-gated (v0.2) ([02c1da8](https://github.com/schubydoo/clauster/commit/02c1da861b793528d39b76367777c08174bc0cd3))
* URL display + QR code for sessions (v0.1 feature 5) ([d1323c4](https://github.com/schubydoo/clauster/commit/d1323c43cd27d43202f7135294cd3baafdb61f8f))
* WebSocket bridge log tail, redacted (v0.1 feature 6) ([5151fea](https://github.com/schubydoo/clauster/commit/5151fea817f2226ae336c13010f6776882542295))


### Bug Fixes

* 4 UI bugs found in live testing ([ce99b3e](https://github.com/schubydoo/clauster/commit/ce99b3e4b981c632376e285b058e6277fd7fa97c))
* address multi-agent review findings (type/config hardening + tests) ([39c6a43](https://github.com/schubydoo/clauster/commit/39c6a43b739844e7118e9441b9891adf318abb3c))
* close two deferred review items (clean backup error + insecure-cookie warning) ([fd8bcd6](https://github.com/schubydoo/clauster/commit/fd8bcd669ce2d2ef86644406ee3c34a6a810b458))


### Build System & Dependencies

* sync uv.lock with pyproject (drop logfire tree, add ruff + pyright) ([48abfcd](https://github.com/schubydoo/clauster/commit/48abfcdba851dee46ab5e367f98a3ea19f6af918))
