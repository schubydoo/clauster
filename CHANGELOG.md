# Changelog

## [0.4.0](https://github.com/schubydoo/clauster/compare/v0.3.0...v0.4.0) (2026-06-04)


### Features

* **runner:** recover reboot-orphaned bridges as resumable stopped cards ([#110](https://github.com/schubydoo/clauster/issues/110)) ([1b0874e](https://github.com/schubydoo/clauster/commit/1b0874e4f729dd12d6cb77486e21508a55abe82b))
* **ui:** per-launch standard|pty resume-mode picker ([#103](https://github.com/schubydoo/clauster/issues/103)) ([6e49f6c](https://github.com/schubydoo/clauster/commit/6e49f6c9fb7dc783360056afa7370f8c76878971))
* **ui:** rename Restart to Resume and add a warned "Start new session" ([#101](https://github.com/schubydoo/clauster/issues/101)) ([ca2e6ab](https://github.com/schubydoo/clauster/commit/ca2e6abfd086db0c45c14ec2cff2a093490ac48c))
* **usage:** add usage.show_cost toggle to hide the cost badge ([#121](https://github.com/schubydoo/clauster/issues/121)) ([bffa1c4](https://github.com/schubydoo/clauster/commit/bffa1c433675577c3c32b2a849bf8ef92b59788e))


### Bug Fixes

* **io:** specify explicit UTF-8 encoding on all file reads/writes ([#122](https://github.com/schubydoo/clauster/issues/122)) ([8427160](https://github.com/schubydoo/clauster/commit/8427160adabbf35c96fdef27a62deef9128238b6))
* make a bridge's resume_mode an instance property, not a live config override ([#100](https://github.com/schubydoo/clauster/issues/100)) ([21ddd26](https://github.com/schubydoo/clauster/commit/21ddd2626ed5719942b52fd732bdfd8d5a1072b3))
* **procutil:** tighten the PID-reuse window in is_live_bridge ([#104](https://github.com/schubydoo/clauster/issues/104)) ([583ec11](https://github.com/schubydoo/clauster/commit/583ec11ed54582c75bfb6f86cd3afb1b649fb37f))
* **recap:** make the recap boundary un-forgeable (prompt-injection hardening) ([#105](https://github.com/schubydoo/clauster/issues/105)) ([9afd544](https://github.com/schubydoo/clauster/commit/9afd544f7a70b0c39174a95638542bdb224cc2b2))
* **trust:** flock the ~/.claude.json read-modify-write (lost-update guard) ([#108](https://github.com/schubydoo/clauster/issues/108)) ([4ebeab5](https://github.com/schubydoo/clauster/commit/4ebeab557dcd29ede31c57a576d5a2c6f86c6b18))


### Performance

* **test:** speed up the suite 48s→14s (xdist + cap the 15s ready-timeout test) ([#111](https://github.com/schubydoo/clauster/issues/111)) ([97f3469](https://github.com/schubydoo/clauster/commit/97f34694e41ed9c129d219c9f95d53b90db38de4))


### Supply Chain & CI

* **Signed Releases (OpenSSF Scorecard):** sign + attach release artifacts — the sdist/wheel are now Sigstore-signed and attached to each GitHub Release via an immutable draft→sign→publish flow ([#114](https://github.com/schubydoo/clauster/issues/114)) ([380656e](https://github.com/schubydoo/clauster/commit/380656e2490bd63b79d6740dcb056177f9664b71))
* **review:** CodeRabbit as the automatic reviewer + Claude as an on-demand `@claude` backup; calibrated `.coderabbit.yaml` ([#120](https://github.com/schubydoo/clauster/issues/120)) ([bc45048](https://github.com/schubydoo/clauster/commit/bc4504878b73ea124325f4ff245d0af0785a73d1)) — building on [#113](https://github.com/schubydoo/clauster/issues/113), [#116](https://github.com/schubydoo/clauster/issues/116), [#117](https://github.com/schubydoo/clauster/issues/117), [#119](https://github.com/schubydoo/clauster/issues/119)
* **security:** move the Trivy image scan to main-push + cron, off PRs ([#112](https://github.com/schubydoo/clauster/issues/112)) ([8fa8a51](https://github.com/schubydoo/clauster/commit/8fa8a510091e4e01976d3537847397f29ff513de))
* **codecov:** tune `codecov.yml` to best practice ([#115](https://github.com/schubydoo/clauster/issues/115)) ([3e78641](https://github.com/schubydoo/clauster/commit/3e786410c6e438770a98f2ef5b45731a421f82af))
* **codecov:** skip the coverage upload on release-please PRs ([#109](https://github.com/schubydoo/clauster/issues/109)) ([040f2ee](https://github.com/schubydoo/clauster/commit/040f2ee61c64d19fc52a43ab83cfed059d9e3402))


### Tests

* **clone:** end-to-end clone-pipeline test (POST → background task → WebSocket progress) ([#106](https://github.com/schubydoo/clauster/issues/106)) ([b1d4b7b](https://github.com/schubydoo/clauster/commit/b1d4b7b977c88df128c546bbbd628217cfedf5f7))
* **runner:** win32 pty-mode guard coverage ([#107](https://github.com/schubydoo/clauster/issues/107)) ([0b85f4f](https://github.com/schubydoo/clauster/commit/0b85f4f68a5507f8896f0aa730bacd1ec5781b4d))

## [0.3.0](https://github.com/schubydoo/clauster/compare/v0.2.2...v0.3.0) (2026-06-03)


### Features

* **docker:** add a Docker Compose quickstart ([#97](https://github.com/schubydoo/clauster/issues/97)) ([e6c914d](https://github.com/schubydoo/clauster/commit/e6c914d22e648ede2dd3c0285a717fdae5f1bef2))
* **doctor:** check that the claude CLI is logged in ([#84](https://github.com/schubydoo/clauster/issues/84)) ([f902f23](https://github.com/schubydoo/clauster/commit/f902f23af6876f1cc95e450c0d4f1447c5e71cfe))


### Bug Fixes

* **runner:** serialize concurrent spawns of the same project ([#91](https://github.com/schubydoo/clauster/issues/91)) ([2dc8eb0](https://github.com/schubydoo/clauster/commit/2dc8eb044aaed3e735627f2feedbce8281a6b298))
* **runner:** stop wiping persisted metadata for untracked projects ([#92](https://github.com/schubydoo/clauster/issues/92)) ([cca1c69](https://github.com/schubydoo/clauster/commit/cca1c69ec29a48c2d034d64cdf5a23e4dca1383a))
* show Restart for stopped pty bridges so true-resume is reachable ([#99](https://github.com/schubydoo/clauster/issues/99)) ([5ea38aa](https://github.com/schubydoo/clauster/commit/5ea38aaa0b2ed92039681be4babe32c7ba9ad465))

## [0.2.2](https://github.com/schubydoo/clauster/compare/v0.2.1...v0.2.2) (2026-06-03)


### Security

* **This is a security release.** A non-loopback bind (e.g. `0.0.0.0` or a LAN IP) could serve the dashboard **unauthenticated** when `auth.enabled` was left at its default `false` — even with a password configured — because the runtime guard only enforces auth when `auth.enabled` is set, while config validation did not require it. The config validator now refuses to start a non-loopback bind unless authentication is actually enforced (`auth.enabled: true` together with `auth.password_required` + a hash, or `auth.reverse_proxy.enabled`; or the explicit `auth.allow_unauthenticated_network` opt-out). All prior releases (≤ 0.2.1) are affected, including the Docker image. **Upgrade, and on any networked deployment set `auth.enabled: true`.** See [GHSA-h4g2-xfmw-q2c9](https://github.com/schubydoo/clauster/security/advisories/GHSA-h4g2-xfmw-q2c9).


### Bug Fixes

* **auth:** refuse non-loopback bind unless auth is actually enforced ([#88](https://github.com/schubydoo/clauster/issues/88)) ([d89d753](https://github.com/schubydoo/clauster/commit/d89d753120c2246eea1838cea9528aa7658eb36f))

## [0.2.1](https://github.com/schubydoo/clauster/compare/v0.2.0...v0.2.1) (2026-06-03)


### Documentation

* absolute GitHub URLs in README so images render on PyPI ([#79](https://github.com/schubydoo/clauster/issues/79)) ([1feef42](https://github.com/schubydoo/clauster/commit/1feef42b91a82b2d31063aa448c90e5a0688fb6a))

## [0.2.0](https://github.com/schubydoo/clauster/compare/v0.1.0...v0.2.0) (2026-06-03)


### Features

* auth foundation — password login, WS auth, reverse-proxy trust, state.json (v0.2) ([b9f40eb](https://github.com/schubydoo/clauster/commit/b9f40eb4081bfd28e0c4eeaf7750840db213e0ae))
* CLAUDE.md viewer/editor (v0.2, spec §5) ([4bb7a6e](https://github.com/schubydoo/clauster/commit/4bb7a6e23e0da8aed70ec36d56f2ebf3513cc7a8))
* **clone:** async clone with live progress over WebSocket (backend, PR A) ([#52](https://github.com/schubydoo/clauster/issues/52)) ([082b804](https://github.com/schubydoo/clauster/commit/082b8046814bde29bf56a4b825fd5adf51d7fcd1))
* cost / token tracking from session transcripts (v0.3) ([842e6dc](https://github.com/schubydoo/clauster/commit/842e6dc831331353a88358161ed598ef976fe51f))
* **docker:** multi-arch GHCR image + trivy-image scan ([#14](https://github.com/schubydoo/clauster/issues/14)) ([113415d](https://github.com/schubydoo/clauster/commit/113415dc98e4fbeedd01ba0246ca0b4feb0500f0))
* **doctor:** warn when a source checkout is behind upstream ([#34](https://github.com/schubydoo/clauster/issues/34)) ([89117ca](https://github.com/schubydoo/clauster/commit/89117cafd4c8c27662d5ac01187a666e63d56499))
* ghost-environment reaper dashboard UI, opt-in (v0.3) ([15d50e5](https://github.com/schubydoo/clauster/commit/15d50e5a75a0baf1f15be938b61859f1825aa5cf))
* ghost-environment reaper, dry-run default (v0.3, spec §11) ([5a5fadd](https://github.com/schubydoo/clauster/commit/5a5fadd496d9d5d186394cd18e6fdd568f9aa081))
* **lint:** add pydocstyle (D) docstring-coverage gate + backfill ([#42](https://github.com/schubydoo/clauster/issues/42)) ([f627039](https://github.com/schubydoo/clauster/commit/f62703903abbceac7b0915e3f272955c77dc9965))
* packaging/ops CLIs — doctor/backup/restore/migrate/install-service + PyInstaller (v0.2) ([b13f5e9](https://github.com/schubydoo/clauster/commit/b13f5e99d8861b253316a9fa7ac5bc49b5f0f36f))
* per-project cost badge on the dashboard (v0.3) ([7d67f94](https://github.com/schubydoo/clauster/commit/7d67f94ef86fe9d9375004e007e7f46869349c16))
* project create + clone (v0.2, spec §5 + §11 clone+trust chain) ([599c57b](https://github.com/schubydoo/clauster/commit/599c57b2127fc3e00a8beb8b2da256dcace09cd5))
* project discovery + dashboard scaffolding (v0.1 feature 1) ([54591cc](https://github.com/schubydoo/clauster/commit/54591cc8c4ea846e35b321787f61bac832d0d433))
* real logout revocation via server-held session epoch (v0.3) ([d0c37a5](https://github.com/schubydoo/clauster/commit/d0c37a5bfbe8be4c555b8ba6999a9c27f1600c86))
* recap prior conversation into a restarted bridge (opt-in) ([#39](https://github.com/schubydoo/clauster/issues/39)) ([1a723f5](https://github.com/schubydoo/clauster/commit/1a723f516e887dd18d846a55c3855acbcc6ac44a))
* resume stopped bridges + surface bridge startup errors ([#36](https://github.com/schubydoo/clauster/issues/36)) ([2f93996](https://github.com/schubydoo/clauster/commit/2f93996e1ce7f20f3110ff344af66a1a2c5e3d95))
* **resume:** PTY true-resume mode (backend slice 1) ([#58](https://github.com/schubydoo/clauster/issues/58)) ([7673d68](https://github.com/schubydoo/clauster/commit/7673d683c5c5fedd5960aeccbf1f298885d94e4d))
* **runner:** auto-enable remote control so bridges skip the y/n prompt ([#29](https://github.com/schubydoo/clauster/issues/29)) ([4698b7f](https://github.com/schubydoo/clauster/commit/4698b7fbeedd9ad95ee74155a9cdfc51ec92b169))
* **runner:** graceful stop on Windows via CTRL_BREAK ([#13](https://github.com/schubydoo/clauster/issues/13)) ([6496f14](https://github.com/schubydoo/clauster/commit/6496f14deb3b43d6c8e004946dd9c296773b38f2))
* SessionRunner — spawn/stop bridges + agents --json cross-check (v0.1 features 2-4) ([71a5965](https://github.com/schubydoo/clauster/commit/71a5965e8c1e11b24dcbc36d6f4fb8a9632a67b2))
* spawn-mode + permission-mode pickers, footgun-gated (v0.2) ([02c1da8](https://github.com/schubydoo/clauster/commit/02c1da861b793528d39b76367777c08174bc0cd3))
* **ui:** connection-lost banner + inline action errors (no silent failures) ([#56](https://github.com/schubydoo/clauster/issues/56)) ([0989f3e](https://github.com/schubydoo/clauster/commit/0989f3e5ccd436e91556715e53d8beaedd18279c))
* **ui:** insert new project cards reactively, no full-page reload ([#55](https://github.com/schubydoo/clauster/issues/55)) ([cbc8398](https://github.com/schubydoo/clauster/commit/cbc8398d89461c93d91232bfe6357cf937f7c800))
* **ui:** live clone progress bar + visible errors (async clone, PR B) ([#53](https://github.com/schubydoo/clauster/issues/53)) ([a9c6e13](https://github.com/schubydoo/clauster/commit/a9c6e13c88a4b57f22860e04cde832a2d211cca1))
* **ui:** rebuild dashboard + login on Tabler (dark/light theme) ([#40](https://github.com/schubydoo/clauster/issues/40)) ([52afe5b](https://github.com/schubydoo/clauster/commit/52afe5b4cbb81d390639f2c737941a194dea61ae))
* **ui:** true-resume badge + recover keeper on pty rediscovery ([#76](https://github.com/schubydoo/clauster/issues/76)) ([9886ca8](https://github.com/schubydoo/clauster/commit/9886ca812b58885d73e246cb30ffd57d1e8b78c7))
* **ui:** vendor Iconoir icons on dashboard actions + theme toggle ([#57](https://github.com/schubydoo/clauster/issues/57)) ([8640c8d](https://github.com/schubydoo/clauster/commit/8640c8d7dc523f7c3ab36b37d94e45445de041ac))
* URL display + QR code for sessions (v0.1 feature 5) ([d1323c4](https://github.com/schubydoo/clauster/commit/d1323c43cd27d43202f7135294cd3baafdb61f8f))
* WebSocket bridge log tail, redacted (v0.1 feature 6) ([5151fea](https://github.com/schubydoo/clauster/commit/5151fea817f2226ae336c13010f6776882542295))


### Bug Fixes

* 4 UI bugs found in live testing ([ce99b3e](https://github.com/schubydoo/clauster/commit/ce99b3e4b981c632376e285b058e6277fd7fa97c))
* address multi-agent review findings (type/config hardening + tests) ([39c6a43](https://github.com/schubydoo/clauster/commit/39c6a43b739844e7118e9441b9891adf318abb3c))
* **auth:** floor session-epoch bump against in-memory value (can't regress) ([#25](https://github.com/schubydoo/clauster/issues/25)) ([04a8549](https://github.com/schubydoo/clauster/commit/04a854985b606a26f42e0773e244cd0eb39b96d9))
* close two deferred review items (clean backup error + insecure-cookie warning) ([fd8bcd6](https://github.com/schubydoo/clauster/commit/fd8bcd669ce2d2ef86644406ee3c34a6a810b458))
* **ops,auth,environments:** atomic restore + IPv6 origin + bounded pagination ([#30](https://github.com/schubydoo/clauster/issues/30)) ([1a6074b](https://github.com/schubydoo/clauster/commit/1a6074b6f07806605ae2aaf8ce97f6052c9f92c9))
* **redact:** mask bare UUIDs (organization_uuid, bridgeId) in the WS log stream ([#51](https://github.com/schubydoo/clauster/issues/51)) ([6c00397](https://github.com/schubydoo/clauster/commit/6c003972206db90cbad68bc9f7446dd035086040))
* **renovate:** match vendored versions.txt via glob, not path-anchored regex ([#48](https://github.com/schubydoo/clauster/issues/48)) ([8410704](https://github.com/schubydoo/clauster/commit/8410704982e59235df5c4b027778d7e0320b4bfa))
* **renovate:** stop ignoring src/clauster/static/vendor via default ignorePaths ([#49](https://github.com/schubydoo/clauster/issues/49)) ([87638ab](https://github.com/schubydoo/clauster/commit/87638ab7345ef95135cdd6045e178dec3fa7d38c))
* **runner,provisioning:** resolve exec paths + harden spawn/stop (audit) ([#17](https://github.com/schubydoo/clauster/issues/17)) ([9aaee4c](https://github.com/schubydoo/clauster/commit/9aaee4c03c2620a204018724408a520ed3554f05))
* **runner:** keep a slow-but-alive bridge STARTING, not a false ERROR ([#27](https://github.com/schubydoo/clauster/issues/27)) ([c72fefe](https://github.com/schubydoo/clauster/commit/c72fefe3092ac424058321900415a6be417a1317))
* **runner:** require env registration before reporting a bridge RUNNING ([#28](https://github.com/schubydoo/clauster/issues/28)) ([7c3fad9](https://github.com/schubydoo/clauster/commit/7c3fad9d3ada016a7d5cdcc2de83e13a99008884))
* **runner:** tolerate unparseable pointer procStart during rediscover ([#23](https://github.com/schubydoo/clauster/issues/23)) ([5a21f92](https://github.com/schubydoo/clauster/commit/5a21f9288b2c29c0e68f7f6ef1b3b7aab5cca2af))
* **security:** trust-gate CLAUDE.md, harden CSRF/throttle/secret/backup (audit) ([#18](https://github.com/schubydoo/clauster/issues/18)) ([2fc01a1](https://github.com/schubydoo/clauster/commit/2fc01a1479b88f698f8c38b3524b9f2c8d97a87d))
* **ui:** relabel "Resume" → "Restart" (it doesn't restore conversation) ([#38](https://github.com/schubydoo/clauster/issues/38)) ([84cff35](https://github.com/schubydoo/clauster/commit/84cff3573322727550241eb1ecda066b2f808b76))
* **usage:** tolerate invalid UTF-8 bytes when parsing transcripts ([#22](https://github.com/schubydoo/clauster/issues/22)) ([565a333](https://github.com/schubydoo/clauster/commit/565a333be18f43cbf65cd09df9c487052fadacda))


### Build System & Dependencies

* sync uv.lock with pyproject (drop logfire tree, add ruff + pyright) ([48abfcd](https://github.com/schubydoo/clauster/commit/48abfcdba851dee46ab5e367f98a3ea19f6af918))
