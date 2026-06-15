# Changelog

## [0.11.0](https://github.com/schubydoo/clauster/compare/v0.10.0...v0.11.0) (2026-06-15)


### Features

* **install:** one-line installers, Scoop/Homebrew/Nix + release auto-bump ([#287](https://github.com/schubydoo/clauster/issues/287)) ([feb666f](https://github.com/schubydoo/clauster/commit/feb666f645f50e962f4ce7964282f6848da8a54a))

## [0.10.0](https://github.com/schubydoo/clauster/compare/v0.9.0...v0.10.0) (2026-06-15)


### Features

* **ops:** make install-service --write install the unit + actionable systemd doctor fix ([#267](https://github.com/schubydoo/clauster/issues/267)) ([bf32d00](https://github.com/schubydoo/clauster/commit/bf32d0071b35811a02d3e459d178bd84a01f0397))
* **sessions:** Forget a stopped session to clear it from Recent/resumable ([#268](https://github.com/schubydoo/clauster/issues/268)) ([7b68db8](https://github.com/schubydoo/clauster/commit/7b68db8cb9b7214b1669ff7fe0cedf2d1202eeb1))


### Bug Fixes

* **recap:** make the SessionStart hook survive a frozen one-file binary ([#279](https://github.com/schubydoo/clauster/issues/279)) ([6caa0ae](https://github.com/schubydoo/clauster/commit/6caa0ae8cdc3edb32904ba2623187b3fc9511302))
* **ui:** clarify launch Mode label + gate Spawn selector to the standard bridge ([#265](https://github.com/schubydoo/clauster/issues/265)) ([fefc3d9](https://github.com/schubydoo/clauster/commit/fefc3d9b1a28c296be9004a5293cfb7cd55556fc))
* **ui:** make detached & hosted Stop/Kill/Resume clickable; honest claude.ai framing ([#266](https://github.com/schubydoo/clauster/issues/266)) ([fdfd326](https://github.com/schubydoo/clauster/commit/fdfd326af97fb52317d030ed27bac60e4f3bf7fa))


### Performance

* **test:** shrink hosted stop-grace so the suite isn't 30s slower ([#275](https://github.com/schubydoo/clauster/issues/275)) ([3a1cd76](https://github.com/schubydoo/clauster/commit/3a1cd764caed15c705c82d1b8343483cffbcd342))

## [0.9.0](https://github.com/schubydoo/clauster/compare/v0.8.0...v0.9.0) (2026-06-14)


### Features

* **agents:** cloud-deregistering stop for background sessions (BG-3) ([#218](https://github.com/schubydoo/clauster/issues/218)) ([c3bae48](https://github.com/schubydoo/clauster/commit/c3bae48a5fff570efac6ee5d954c8ed55cb4fbc7))
* **agents:** dispatch a `claude --bg [--rc <name>]` background session (BG-2) ([#215](https://github.com/schubydoo/clauster/issues/215)) ([83e2ad4](https://github.com/schubydoo/clauster/commit/83e2ad47767d453f6f987675ff0986308bf0a028))
* **agents:** read-only background-agents panel (BG-1) ([#214](https://github.com/schubydoo/clauster/issues/214)) ([a755723](https://github.com/schubydoo/clauster/commit/a75572306d21ef3b820b3a248bb63387f9a89ca9))
* **agents:** wire dispatch + stop buttons into the bg-agents panel (BG-4) ([#220](https://github.com/schubydoo/clauster/issues/220)) ([99fb3eb](https://github.com/schubydoo/clauster/commit/99fb3eb16d0298a9393bac7fbcb7004a723d18fc))
* configurable usage badge — currency conversion + tokens-only mode ([#192](https://github.com/schubydoo/clauster/issues/192)) ([d53a7f5](https://github.com/schubydoo/clauster/commit/d53a7f59232f723edce0f6ae7d4df479fdc2f253))
* **hosted:** --resume respawn of a lost hosted session (CL-7) ([#236](https://github.com/schubydoo/clauster/issues/236)) ([671eee7](https://github.com/schubydoo/clauster/commit/671eee7288eecff8a551741ab51988105dc0b001))
* **hosted:** claustrum daemon connect-or-spawn lifecycle (CL-2) ([#229](https://github.com/schubydoo/clauster/issues/229)) ([da8f72d](https://github.com/schubydoo/clauster/commit/da8f72d794a664ca805e8214ff5c2cabf52548c1))
* **hosted:** claustrum NDJSON JSON-RPC client + fake daemon fixture (CL-1) ([#224](https://github.com/schubydoo/clauster/issues/224)) ([dff6c33](https://github.com/schubydoo/clauster/commit/dff6c332af4cee59a8d03f4c798664631246a99f))
* **hosted:** hosted-channel session engine (CL-4a) ([#230](https://github.com/schubydoo/clauster/issues/230)) ([d9ab7e6](https://github.com/schubydoo/clauster/commit/d9ab7e67b8652e24c591d91e949256d450ff707e))
* **hosted:** live-view UI for hosted sessions (CL-4c) ([#233](https://github.com/schubydoo/clauster/issues/233)) ([234e525](https://github.com/schubydoo/clauster/commit/234e52599e67ef9b0b0bff1f0ed2469947bc89d5))
* **hosted:** orphan detection + recovery after a daemon restart (CL-8) ([#237](https://github.com/schubydoo/clauster/issues/237)) ([6a64eef](https://github.com/schubydoo/clauster/commit/6a64eefff57fec8caa90d9fdb7d51f3927ab7d3f))
* **hosted:** permissions approve/deny UI for hosted sessions (CL-5) ([#234](https://github.com/schubydoo/clauster/issues/234)) ([d5acb2d](https://github.com/schubydoo/clauster/commit/d5acb2d51aeae0933b44b4889cc7a0b7779938f4))
* **hosted:** persist + reattach hosted sessions across restarts (CL-6) ([#235](https://github.com/schubydoo/clauster/issues/235)) ([ea45088](https://github.com/schubydoo/clauster/commit/ea450880d6fb79c1a8520507e69c119de5e689b6))
* **hosted:** wire the hosted channel into the app (CL-4b) ([#231](https://github.com/schubydoo/clauster/issues/231)) ([2e219a3](https://github.com/schubydoo/clauster/commit/2e219a3171166b1574bf9d1f125c5f284f8345c5))
* **logs:** redact the session URL in the on-disk bridge log (redact_session_url) ([#200](https://github.com/schubydoo/clauster/issues/200)) ([aa60bf6](https://github.com/schubydoo/clauster/commit/aa60bf660fe88d4597c9b648a04d7322fd1f1783))
* notify on bridge crash via Apprise (optional extra) ([#197](https://github.com/schubydoo/clauster/issues/197)) ([8a1d944](https://github.com/schubydoo/clauster/commit/8a1d9441f7c1b7fa388278cf226fab3f89d1c573))
* per-project preflight endpoint (GET /api/projects/{name}/preflight) ([#193](https://github.com/schubydoo/clauster/issues/193)) ([18c0ec6](https://github.com/schubydoo/clauster/commit/18c0ec647eeab2eed88a96e964bc7ffba4c6f152))
* **service:** default KillMode=process so pty bridges survive a restart ([#206](https://github.com/schubydoo/clauster/issues/206)) ([992f84b](https://github.com/schubydoo/clauster/commit/992f84b3c84a71e42f331cc28872e51c1da31a57))
* **ui:** two-zone dashboard redesign; migrate icons to Tabler Icons ([#248](https://github.com/schubydoo/clauster/issues/248)) ([c060d93](https://github.com/schubydoo/clauster/commit/c060d93c576ffaf815d6344ac830b273e3195f4e))


### Bug Fixes

* **agents:** don't report a clean stop when no live worker was found ([#255](https://github.com/schubydoo/clauster/issues/255)) ([8dfd1e3](https://github.com/schubydoo/clauster/commit/8dfd1e3e959b6858b7d39112d005fe16ec765b80))
* **app:** end send-only WS handlers on client disconnect — ghost tasks stalled shutdown ([#243](https://github.com/schubydoo/clauster/issues/243)) ([397d84b](https://github.com/schubydoo/clauster/commit/397d84b19ab0a4581c9e9c422ed6546c790a21ee))
* **app:** enforce bypass-permissions ceiling on hosted + bg-agent channels ([#249](https://github.com/schubydoo/clauster/issues/249)) ([90d74c2](https://github.com/schubydoo/clauster/commit/90d74c2abf19c75131b0765ba7ab5dc5dc308097))
* **app:** friendly HTML 404 for browser navigation; unify project-not-found wording ([#247](https://github.com/schubydoo/clauster/issues/247)) ([a6ad579](https://github.com/schubydoo/clauster/commit/a6ad57961134269da0cb50c388fe74c80b88d198))
* **auth:** fsync parent dir when creating session.secret ([#261](https://github.com/schubydoo/clauster/issues/261)) ([8fafd6b](https://github.com/schubydoo/clauster/commit/8fafd6b94569bde84020ab87c2a370fb992ea732))
* **ci:** always run CodeQL so docs-only PRs aren't blocked by code-scanning rule ([#196](https://github.com/schubydoo/clauster/issues/196)) ([71a0b66](https://github.com/schubydoo/clauster/commit/71a0b669f033e949dae4e315762f66e187540298))
* correctness/robustness batch from a clean-room audit ([#252](https://github.com/schubydoo/clauster/issues/252)) ([4e097e3](https://github.com/schubydoo/clauster/commit/4e097e3e54da0753efb697be1561b1d3ca536439))
* **docker:** bump base image digest to clear stale OpenSSL CVEs ([#216](https://github.com/schubydoo/clauster/issues/216)) ([231cd8d](https://github.com/schubydoo/clauster/commit/231cd8d20e8cc98779d59db43c7fdf7af43d752f))
* don't render the live metrics line twice in rows layout ([#190](https://github.com/schubydoo/clauster/issues/190)) ([535efdd](https://github.com/schubydoo/clauster/commit/535efdd0a88e0eafe9803e87e41bbe3c81c4e088))
* **hosted:** handle an over-limit claustrum frame without killing the reader ([#256](https://github.com/schubydoo/clauster/issues/256)) ([dc0249e](https://github.com/schubydoo/clauster/commit/dc0249ef88c60f56f3ec92a9c1eed9487d3d4874))
* **hosted:** permission allow updatedInput + stop exit-latch race ([#242](https://github.com/schubydoo/clauster/issues/242)) ([a84b937](https://github.com/schubydoo/clauster/commit/a84b9374d5fd9dd57b3c1a4e4aeeb36457ea8722))
* **hosted:** resolve parked requests on exit + fix live-view double-wire ([#254](https://github.com/schubydoo/clauster/issues/254)) ([0f83fe1](https://github.com/schubydoo/clauster/commit/0f83fe12ebaafe40f445e971cbfef005f84b65ea))
* **hosted:** scrub claustrum's daemonize sentinel from the spawned daemon env ([#241](https://github.com/schubydoo/clauster/issues/241)) ([02242cb](https://github.com/schubydoo/clauster/commit/02242cbb2ce425086236b014d08be450f5bd5c0f))
* **hosted:** surface terminal state in live-view; stop the dead-session reconnect loop ([#245](https://github.com/schubydoo/clauster/issues/245)) ([fb51cf4](https://github.com/schubydoo/clauster/commit/fb51cf42a5345ed756e45c16fe9d474b08496416))
* **inspector:** gate cwd attribution on agent-view kind/state ([#213](https://github.com/schubydoo/clauster/issues/213)) ([c116fe9](https://github.com/schubydoo/clauster/commit/c116fe924e9c35f01f9e3b6c19f34b29f10e23f7))
* **logs:** whole first WS tail line + 0600 verbatim bridge log when redaction off ([#259](https://github.com/schubydoo/clauster/issues/259)) ([f6f2b30](https://github.com/schubydoo/clauster/commit/f6f2b3048975a265650acccc1ef4f409441c5517))
* scrub Clauster secrets from every spawned child environment ([#253](https://github.com/schubydoo/clauster/issues/253)) ([926f315](https://github.com/schubydoo/clauster/commit/926f315583aff2561f7b3e0c2f03419060ede80e))
* **state:** harden state writes — 0700 dir, 0600 atomic temp, fsync durability ([#258](https://github.com/schubydoo/clauster/issues/258)) ([bc09bee](https://github.com/schubydoo/clauster/commit/bc09beeb86e0e8ae864c559f6064d56774c27d73))
* **ui:** clear stale New-project dialog state on close, mode-switch, and edit ([#246](https://github.com/schubydoo/clauster/issues/246)) ([b000f75](https://github.com/schubydoo/clauster/commit/b000f751cae162c62d77854cc1c27e0d27d94d6f))
* **ui:** label launch controls + guard the launch button against double-submit ([#260](https://github.com/schubydoo/clauster/issues/260)) ([be3efde](https://github.com/schubydoo/clauster/commit/be3efde00ef3844dc169720fe31b9ce643c07222))
* **ui:** render the Active status rail + fix keyboard focus order ([#250](https://github.com/schubydoo/clauster/issues/250)) ([a82cbe8](https://github.com/schubydoo/clauster/commit/a82cbe80935cd511d19716569e8f437c1d3a5e94))
* **ui:** restore the Tabler + Alpine.js attribution in the dashboard footer ([#198](https://github.com/schubydoo/clauster/issues/198)) ([9366c8b](https://github.com/schubydoo/clauster/commit/9366c8b46feece6f7e1cf10ed08999a55d181008))
* **ui:** status-presentation parity, untrusted indicator, bypass-confirm gating ([#251](https://github.com/schubydoo/clauster/issues/251)) ([2becb73](https://github.com/schubydoo/clauster/commit/2becb738f768530eabbb31c76343c9ce662a1f04))
* **usage:** tolerate malformed token values instead of 500-ing the rollup ([#257](https://github.com/schubydoo/clauster/issues/257)) ([dce4efc](https://github.com/schubydoo/clauster/commit/dce4efc16415781b880f9258f84412fdc08f128a))

## [0.8.0](https://github.com/schubydoo/clauster/compare/v0.7.0...v0.8.0) (2026-06-07)


### Features

* add gated Prometheus /metrics endpoint ([#178](https://github.com/schubydoo/clauster/issues/178)) ([adac1c6](https://github.com/schubydoo/clauster/commit/adac1c65ea54ec5e486624b57e8a2102846a8fab))
* add read-only /api/widget summary endpoint ([#179](https://github.com/schubydoo/clauster/issues/179)) ([b38f71d](https://github.com/schubydoo/clauster/commit/b38f71dbc409f3092f0a7b439cb4317164ecd24f))
* **ui:** cards ⇄ rows dashboard layout toggle ([#173](https://github.com/schubydoo/clauster/issues/173)) ([da027f0](https://github.com/schubydoo/clauster/commit/da027f07e0338f94b6450eb22f689ba188ace231))
* **ui:** honest currency label on the cost badge (symbol only for USD) ([#167](https://github.com/schubydoo/clauster/issues/167)) ([ce5321c](https://github.com/schubydoo/clauster/commit/ce5321cdb6f54ab28494cc579a3f497094529812))
* **ui:** live per-bridge resource metrics (CPU / memory / disk) ([#172](https://github.com/schubydoo/clauster/issues/172)) ([bc2992e](https://github.com/schubydoo/clauster/commit/bc2992e5eaf4bf505f3591e079f1aa770a207bda))


### Bug Fixes

* **ci:** stop [@claude](https://github.com/claude) review from cancelling itself ([#183](https://github.com/schubydoo/clauster/issues/183)) ([abbd8bf](https://github.com/schubydoo/clauster/commit/abbd8bf6fb6f2a1dc6410290e04bd25739ee3d2f))
* **ui:** correct and clarify the permission-mode tooltip ([#165](https://github.com/schubydoo/clauster/issues/165)) ([5e6539e](https://github.com/schubydoo/clauster/commit/5e6539eeec8acf2b4329a24a6ac2171bc62d4b7b))

## [0.7.0](https://github.com/schubydoo/clauster/compare/v0.6.0...v0.7.0) (2026-06-06)


### Features

* **ui:** actionable empty-state CTA ([#159](https://github.com/schubydoo/clauster/issues/159)) ([f382c1a](https://github.com/schubydoo/clauster/commit/f382c1aec4b5fddc03f7ba4169932b50efb50e50))
* **ui:** tooltips pass across the dashboard card ([#158](https://github.com/schubydoo/clauster/issues/158)) ([68c4009](https://github.com/schubydoo/clauster/commit/68c4009fbcd59c7233d6cff646b208ce8e804da0))


### Bug Fixes

* address four low-severity review findings ([#155](https://github.com/schubydoo/clauster/issues/155)) ([824c234](https://github.com/schubydoo/clauster/commit/824c23416a5ef26656bd7ed90fad9f764c27dce4))
* stop misclassifying a live clauster-launched pty bridge as external ([#153](https://github.com/schubydoo/clauster/issues/153)) ([3d12a6b](https://github.com/schubydoo/clauster/commit/3d12a6bfe0615e569518daeade90f78605c741cb))

## [0.6.0](https://github.com/schubydoo/clauster/compare/v0.5.0...v0.6.0) (2026-06-05)


### Features

* **ui:** redesign the project card — clearer hierarchy, one primary action ([#143](https://github.com/schubydoo/clauster/issues/143)) ([8723498](https://github.com/schubydoo/clauster/commit/87234980fa2cbdb4c159675ee8aa6c831fa3d8a2))
* **ui:** trust-on-start — prompt to trust a directory at launch ([#144](https://github.com/schubydoo/clauster/issues/144)) ([110da36](https://github.com/schubydoo/clauster/commit/110da3600d4e1678b133b5de0c7b8c949df28505))


### Bug Fixes

* **doctor:** suppress the false "port in use" warning in the dashboard ([#142](https://github.com/schubydoo/clauster/issues/142)) ([5a56c5f](https://github.com/schubydoo/clauster/commit/5a56c5f826e833ffac4fb9d5a50182a196d01eaf))

## [0.5.0](https://github.com/schubydoo/clauster/compare/v0.4.0...v0.5.0) (2026-06-05)


### Features

* **api:** GET /api/doctor — surface system readiness as JSON ([#127](https://github.com/schubydoo/clauster/issues/127)) ([070a39c](https://github.com/schubydoo/clauster/commit/070a39c275a5fa1b35331eb4d22cb8118fe7d0ef))
* **cli:** instance_name — retitle process clauster[&lt;name&gt;] for ps/pgrep ([#130](https://github.com/schubydoo/clauster/issues/130)) ([254379d](https://github.com/schubydoo/clauster/commit/254379d8cf4cb6d30636b885bef3a7c4b7d9d2e1))
* **pty:** recover the "Open session" deep link on a --continue resume ([44d58e4](https://github.com/schubydoo/clauster/commit/44d58e48671a7a2010e00a986f5fbd658f399b50))
* **pty:** recover the Open-session deep link on a --continue resume ([#135](https://github.com/schubydoo/clauster/issues/135)) ([44d58e4](https://github.com/schubydoo/clauster/commit/44d58e48671a7a2010e00a986f5fbd658f399b50))
* **ui:** distinguish "Interrupted" from "Stopped" on the card ([91d5c87](https://github.com/schubydoo/clauster/commit/91d5c87f39bd4a04f7ce55bb3b3d9e8b85607be9))
* **ui:** distinguish "Interrupted" from "Stopped" on the dashboard card ([#136](https://github.com/schubydoo/clauster/issues/136)) ([91d5c87](https://github.com/schubydoo/clauster/commit/91d5c87f39bd4a04f7ce55bb3b3d9e8b85607be9))
* **ui:** system-readiness (preflight) panel on the dashboard ([#129](https://github.com/schubydoo/clauster/issues/129)) ([d752cda](https://github.com/schubydoo/clauster/commit/d752cda9d9d6a99866bceeeed2301b1f3336444d))


### Bug Fixes

* **pty:** --continue resume reads "Failed to start" while actually running ([#134](https://github.com/schubydoo/clauster/issues/134)) ([c39829c](https://github.com/schubydoo/clauster/commit/c39829ca9ebe3f6417f370499ef08397c30fc463))
* **pty:** a --continue resume must not read "Failed to start" while alive ([c39829c](https://github.com/schubydoo/clauster/commit/c39829ca9ebe3f6417f370499ef08397c30fc463))
* **runner:** a phantom STOPPED instance must not shadow a live external bridge ([c08395b](https://github.com/schubydoo/clauster/commit/c08395b9274108691f40780ddd52a98587fd7225))
* **runner:** phantom STOPPED instance shadows a live external bridge ([#133](https://github.com/schubydoo/clauster/issues/133)) ([c08395b](https://github.com/schubydoo/clauster/commit/c08395b9274108691f40780ddd52a98587fd7225))

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
