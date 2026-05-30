# Changelog

## [0.2.0](https://github.com/schubydoo/clauster/compare/v0.1.0...v0.2.0) (2026-05-30)


### Features

* auth foundation — password login, WS auth, reverse-proxy trust, state.json (v0.2) ([2032464](https://github.com/schubydoo/clauster/commit/203246469aad976c64917a256ef4cf8a3da77158))
* CLAUDE.md viewer/editor (v0.2, spec §5) ([88ce29b](https://github.com/schubydoo/clauster/commit/88ce29bf355da784c8a4ac212295d7508faa6c02))
* cost / token tracking from session transcripts (v0.3) ([5290e30](https://github.com/schubydoo/clauster/commit/5290e30e4260559d93e29a087d4cd22d19ffc0b5))
* ghost-environment reaper dashboard UI, opt-in (v0.3) ([e20271e](https://github.com/schubydoo/clauster/commit/e20271e1a6802d7f2c7f99aca8e3024a7dec4c23))
* ghost-environment reaper, dry-run default (v0.3, spec §11) ([c681e9b](https://github.com/schubydoo/clauster/commit/c681e9b43594f9b48f9cecc6194706bbaf083b11))
* packaging/ops CLIs — doctor/backup/restore/migrate/install-service + PyInstaller (v0.2) ([6276491](https://github.com/schubydoo/clauster/commit/62764917ccfc8655b92d8e80f4d13f89787dc095))
* per-project cost badge on the dashboard (v0.3) ([af66c79](https://github.com/schubydoo/clauster/commit/af66c7952bbb0c5049ec7897033eed25e7b7d713))
* project create + clone (v0.2, spec §5 + §11 clone+trust chain) ([3943849](https://github.com/schubydoo/clauster/commit/39438497a9782fc3794ef8592b12c24df017857f))
* project discovery + dashboard scaffolding (v0.1 feature 1) ([481fc0a](https://github.com/schubydoo/clauster/commit/481fc0a48d1a26a68a829ebe16a4fb321c2c27e5))
* real logout revocation via server-held session epoch (v0.3) ([c7f56d8](https://github.com/schubydoo/clauster/commit/c7f56d8f4740848e6f853f2a60b82019d4f588b8))
* SessionRunner — spawn/stop bridges + agents --json cross-check (v0.1 features 2-4) ([3b6b4af](https://github.com/schubydoo/clauster/commit/3b6b4af935c2122d9a2bdac3c203dd2e891ff3cc))
* spawn-mode + permission-mode pickers, footgun-gated (v0.2) ([49f3d89](https://github.com/schubydoo/clauster/commit/49f3d89b7fad279dc63a2a0a2cf31c5411b17ee6))
* URL display + QR code for sessions (v0.1 feature 5) ([a879a40](https://github.com/schubydoo/clauster/commit/a879a4076fa6288ccef3ee448b99191b111fc58f))
* WebSocket bridge log tail, redacted (v0.1 feature 6) ([5a4eb80](https://github.com/schubydoo/clauster/commit/5a4eb80342d3c5ac2272f959ea30a25dbce68d06))


### Bug Fixes

* 4 UI bugs found in live testing ([64cf380](https://github.com/schubydoo/clauster/commit/64cf3803505ac8aa7c504188a8e728925c7e5777))
* address multi-agent review findings (type/config hardening + tests) ([a62d017](https://github.com/schubydoo/clauster/commit/a62d01769d4a1768df471b505567d43b6d4cc7f6))
* close two deferred review items (clean backup error + insecure-cookie warning) ([9c947f7](https://github.com/schubydoo/clauster/commit/9c947f702db4cfde3df988bf8ea3621f635e3f4f))


### Build System & Dependencies

* sync uv.lock with pyproject (drop logfire tree, add ruff + pyright) ([7fe8f26](https://github.com/schubydoo/clauster/commit/7fe8f26631d7e4983b1f54babbd1b3aa50eca458))
