# Changelog

## 0.12.9 (2026-06-29)

[Compare with 0.12.8](https://github.com/schubydoo/clauster/compare/v0.12.8...v0.12.9)

### Features

- Confirm before cancelling an in-progress clone, and reattach a second browser tab to its live progress instead of showing nothing. ([#708](https://github.com/schubydoo/clauster/pull/708))
- Make the `log_format` setting editable from the in-app config editor. ([#705](https://github.com/schubydoo/clauster/pull/705))
- Add an opt-in, off-by-default surface for editing Claude Code hooks (project + user scope) from the config editor. ([#740](https://github.com/schubydoo/clauster/pull/740))
- Add an opt-in, off-by-default surface for editing MCP server config (project + user scope) — no dashboard UI yet. ([#732](https://github.com/schubydoo/clauster/pull/732))
- Add an opt-in, off-by-default surface for editing permission rules — no dashboard UI yet. ([#738](https://github.com/schubydoo/clauster/pull/738))
- Internal: lay the groundwork (off-by-default gate, type-the-name confirm, automatic secret redaction) that the new hooks/MCP/permissions editors build on. ([#706](https://github.com/schubydoo/clauster/pull/706))
- Add `clauster mcp`, a read-only MCP server exposing session status to any MCP client. ([#710](https://github.com/schubydoo/clauster/pull/710))
- Add an opt-in `CLAUSTER_PYTE_PATH` environment variable so standalone-binary users can enable the live terminal view by installing `pyte` separately. ([#702](https://github.com/schubydoo/clauster/pull/702))
- Live-tail a running session's transcript in the read-only viewer as the agent works. ([#709](https://github.com/schubydoo/clauster/pull/709))

### Fixes

- Harden the dashboard's Content-Security-Policy further by removing `unsafe-inline` from `style-src`. ([#635](https://github.com/schubydoo/clauster/pull/635))
- Fix pty bridges intermittently showing "No web link — use Logs" instead of the actual connect link. ([#721](https://github.com/schubydoo/clauster/pull/721))
- Unify the launch and permission mode labels into a single source, so the picker, inline UI, and config editor never disagree. ([#729](https://github.com/schubydoo/clauster/pull/729))
- Simplify the launch flow into a single-screen popover: advanced options tuck under "More options," and Desktop stays the default launch mode. ([#731](https://github.com/schubydoo/clauster/pull/731))
- Fix a failed trust-on-start leaving the launch popover hidden instead of showing the error. ([#739](https://github.com/schubydoo/clauster/pull/739))
- Add a dismissible first-run orientation card explaining what Clauster is and how to start. ([#728](https://github.com/schubydoo/clauster/pull/728))
- Fix a freshly-spawned bridge's session briefly appearing as an unmanaged "phantom" row while the bridge is still starting. ([#714](https://github.com/schubydoo/clauster/pull/714))
- Internal: harden the atomic file-writer against a rare file-descriptor leak on write failure. ([#718](https://github.com/schubydoo/clauster/pull/718))
- Hide a deprecated config field once it's removed from disk, and stop `config reconcile` from re-offering an already-set replacement. ([#703](https://github.com/schubydoo/clauster/pull/703))
- Fix the progress-bar fill and non-name sort layout breaking under the stricter `style-src` CSP. ([#742](https://github.com/schubydoo/clauster/pull/742))
- Fix the live terminal failing to render under the stricter `style-src` CSP. ([#743](https://github.com/schubydoo/clauster/pull/743))
- Correct the docs: the live terminal IS available on the standalone binary via `CLAUSTER_PYTE_PATH`; the install guide now also lists `clauster hash-metrics-token` and `clauster config reconcile`. ([#717](https://github.com/schubydoo/clauster/pull/717))
- Fix a rare mis-render in the hosted live-event stream past 1000 lines. ([#720](https://github.com/schubydoo/clauster/pull/720))
- Fix a 500 error when stopping a hosted session that a concurrent forget/resume already removed. ([#715](https://github.com/schubydoo/clauster/pull/715))
- Fix `clauster keepers` mislabeling live, dashboard-managed keepers as orphans, risking `--kill` reaping them. ([#719](https://github.com/schubydoo/clauster/pull/719))
- Point the live terminal's "unavailable" message on the standalone binary at the real fix (`pip`/`uv` install with `[pty]`) instead of a dead end. ([#700](https://github.com/schubydoo/clauster/pull/700))
- Internal: speed up the dashboard's project-sort endpoint by batching its per-project queries. ([#716](https://github.com/schubydoo/clauster/pull/716))
- Replace ad hoc emoji in the dashboard with consistent Tabler icons. ([#704](https://github.com/schubydoo/clauster/pull/704))

## 0.12.8 (2026-06-28)

[Compare with 0.12.7](https://github.com/schubydoo/clauster/compare/v0.12.7...v0.12.8)

### Features

- Add a Cancel button to the in-progress clone flow, with a confirmation toast when a clone is cancelled. ([#684](https://github.com/schubydoo/clauster/pull/684))
- Add an optional `tls` config block so Clauster can terminate HTTPS directly using an existing certificate and key, validated fail-closed at load and at startup, with a non-fatal warning if the private key file is readable by others. ([#695](https://github.com/schubydoo/clauster/pull/695))

### Fixes

- Fix several inaccuracies in the public docs and add an automated check to prevent them recurring. ([#683](https://github.com/schubydoo/clauster/pull/683))
- Fix Interactive Session (PTY) launches failing under the standalone binary. ([#697](https://github.com/schubydoo/clauster/pull/697))
- Rename session-type terminology across the UI and docs to match Anthropic's Remote Control vocabulary (Server Mode / Interactive Session / Background Agent / Direct Session). ([#681](https://github.com/schubydoo/clauster/pull/681))

## 0.12.7 (2026-06-27)

[Compare with 0.12.6](https://github.com/schubydoo/clauster/compare/v0.12.6...v0.12.7)

### Features

- Add an in-app "Restart Clauster" action to the config editor so a saved config change can be applied without dropping to a shell, exposed via an auth-gated `POST /api/restart`. ([#637](https://github.com/schubydoo/clauster/pull/637))
- Add a browser-notifications channel with per-channel and per-event toggles for ready, stop, permission-needed, session-ended, and reconnect-failed events. ([#636](https://github.com/schubydoo/clauster/pull/636))
- Add `clauster config reconcile`, an interactive CLI command that removes deprecated config keys and writes their replacements. ([#650](https://github.com/schubydoo/clauster/pull/650))
- Add a server-side cancel for an in-progress clone so the UI abort actually stops the git transfer. ([#633](https://github.com/schubydoo/clauster/pull/633))
- Badge a transcript as live in the read-only viewer when its session id maps to a currently-running bridge, agent, or hosted session. ([#653](https://github.com/schubydoo/clauster/pull/653))
- Scale the read-only live terminal to fit the panel width on narrow viewports (e.g. phones). ([#654](https://github.com/schubydoo/clauster/pull/654))

### Fixes

- Add a copy-paste Claude Code install + `claude login` step to the Quickstart guide, and make the doctor version-FAIL message suggest `claude update`. ([#649](https://github.com/schubydoo/clauster/pull/649))
- Fix the project-sort cap: changing sort no longer flashes the full list, and returning to A–Z restores the 6-row cap and "Show all N" toggle. ([#655](https://github.com/schubydoo/clauster/pull/655))
- Correct the Claude Code install docs (native installer, not the deprecated npm package) and the clone dialog's helper text, which now names the `clone.allow_private_hosts` config key and clarifies that cloning only fetches files. ([#658](https://github.com/schubydoo/clauster/pull/658))
- Project sort: selecting a non-name sort (Last used / Cost) no longer uncaps the list past the 6-row limit. ([#661](https://github.com/schubydoo/clauster/pull/661))
- Lower the default `claude.agents_json_poll_interval_seconds` from 300 to 30, so session liveness and crash detection refresh within ~30 seconds instead of up to 5 minutes. ([#662](https://github.com/schubydoo/clauster/pull/662))
- Fix the in-app Restart: the page reloads once the server is back (no more stuck "Restarting…"), and the confirmation now correctly says running sessions survive the restart instead of warning they end. ([#666](https://github.com/schubydoo/clauster/pull/666))
- Browser notifications now prompt for permission the moment you enable the channel instead of only after a reload, and a failed bridge resume only raises the "reconnect failed" notification when the bridge genuinely could not restart. ([#668](https://github.com/schubydoo/clauster/pull/668))
- The config editor now flags when browser notifications can't be delivered (non-HTTPS connection, unsupported browser, or blocked permission) and disables the toggle instead of silently offering a setting that won't work. ([#675](https://github.com/schubydoo/clauster/pull/675))
- `clauster config reconcile --dry-run` is now non-interactive: it prints the plan and writes nothing without prompting. ([#667](https://github.com/schubydoo/clauster/pull/667))

## 0.12.6 (2026-06-27)

[Compare with 0.12.5](https://github.com/schubydoo/clauster/compare/v0.12.5...v0.12.6)

### Fixes

- Fix the in-app config editor's dropdowns (Launch mode, Usage badge mode, and every other selector) showing the first option instead of the actual saved value, which also left Save greyed out after re-picking the real value. ([#644](https://github.com/schubydoo/clauster/pull/644))
- Fix the config editor showing stale pre-save values after a save until a restart — `GET /api/config` now reads current values from disk instead of the in-memory startup config, so a successful save no longer looks reverted (the change still only takes effect on restart). ([#645](https://github.com/schubydoo/clauster/pull/645))
- Fix the dashboard's per-bridge live-session count never appearing for a `spawn_mode: worktree` bridge, whose sessions run in per-project git worktrees and were previously misclassified as external/untracked. ([#646](https://github.com/schubydoo/clauster/pull/646))

## 0.12.5 (2026-06-27)

[Compare with 0.12.4](https://github.com/schubydoo/clauster/compare/v0.12.4...v0.12.5)

### Features

- Add an opt-in trusted-header reverse-proxy mode (`auth.reverse_proxy.require_hmac: false`) so SSO proxies that don't sign a per-request HMAC (Authelia, authentik, Caddy, Traefik, oauth2-proxy) can authenticate via `trusted_ips` + `user_header` alone, and make `reverse_proxy.enabled` without `trusted_ips` fail fast at startup instead of silently authenticating no one. ([#617](https://github.com/schubydoo/clauster/pull/617))
- Add a read-only in-dashboard transcript viewer: a per-project "View transcript" button opens a modal listing each session's history (newest-first) with paginated, redacted turns. ([#611](https://github.com/schubydoo/clauster/pull/611))
- Add the read-only live PTY terminal view: when `claude.pty_screen_enabled` is on, each pty bridge gets a "Live terminal" button that streams a redacted, auth-gated view of the session over `/ws/pty-screen`, and the flag is now toggleable from the in-app config editor. ([#640](https://github.com/schubydoo/clauster/pull/640))
- List every live session under a standard `claude remote-control` bridge: an Active-session card now expands to enumerate each working session with a short-UUID label, uptime, and a per-session deep link into the Claude web app. ([#618](https://github.com/schubydoo/clauster/pull/618))
- Add an in-app help page: a `?` icon in the navbar opens a panel covering launch modes, permission modes, the dashboard zones, and session types, with a link to the README. ([#619](https://github.com/schubydoo/clauster/pull/619))
- When a non-git project's default spawn mode is `worktree`, the "Run Claude here" popover now falls back to `same-dir` and shows a note rather than letting the server reject the spawn, keeping the picker and trust-on-start confirm reachable. ([#615](https://github.com/schubydoo/clauster/pull/615))
- Add a sort-direction toggle (newest-first / oldest-first) and a search box to the transcript viewer; search only matches against already-redacted content, so it can never reveal a masked secret. ([#616](https://github.com/schubydoo/clauster/pull/616))
- Add an `instance_defaults.verbose` config toggle (default off, editable from the config editor) for detailed connection/session logs on standard bridges; pty bridges are intentionally never passed `--verbose`. ([#642](https://github.com/schubydoo/clauster/pull/642))

### Fixes

- Rename the "Fire-and-forget" / `detached` session type to "Background" consistently across the launch picker, filter chip, and card badge. ([#567](https://github.com/schubydoo/clauster/pull/567))
- Show the live-session list from the first live session on a standard bridge (not only at two or more), giving each session its own `claude.ai/code` deep link; the toggle label is now singular/plural-aware. ([#622](https://github.com/schubydoo/clauster/pull/622))
- Fix two dashboard error-UX papercuts: the default-session Stop button now asks for confirmation like every other destructive action, and error toasts persist until dismissed instead of auto-vanishing after 4.5 seconds. ([#627](https://github.com/schubydoo/clauster/pull/627))
- Fix the config editor's "restart Clauster to apply" help link, which pointed at a broken README anchor; it now targets the Operations runbook's restart section. ([#624](https://github.com/schubydoo/clauster/pull/624))
- `clauster.yml.example` now uses `projects_root: ~/code` (matching every prose doc) instead of `/srv/projects`, which contradicted the docs and hard-failed validation if copied as-is on a box without that directory. ([#625](https://github.com/schubydoo/clauster/pull/625))
- Correct the `webhooks.block_private_targets` config-field description, which incorrectly claimed DNS hostnames are not resolved (the SSRF guard does resolve them); documentation only, no behavior change. ([#598](https://github.com/schubydoo/clauster/pull/598))
- a11y: the clone-progress bar now announces its progress to screen readers/assistive tech via proper ARIA value attributes. ([#607](https://github.com/schubydoo/clauster/pull/607))
- The "Recap prior transcript on restart" config toggle is now always editable instead of greyed out when the default launch mode isn't `standard`; an informational note explains it applies to standard bridges only. ([#600](https://github.com/schubydoo/clauster/pull/600))
- Update the README's "First bridge in 60 seconds" walkthrough to match the current UI (button labels and the actual relaunch steps). ([#605](https://github.com/schubydoo/clauster/pull/605))

### Security

- Redact a failed clone's `error_detail` (git stderr) on the progress WebSocket, matching the redaction already applied on the clone-done webhook path. ([#606](https://github.com/schubydoo/clauster/pull/606))

### Build System & Dependencies

- Internal: vendor xterm.js 6.0.0 as the foundation for the upcoming live pty terminal view (no user-facing behavior yet). ([#631](https://github.com/schubydoo/clauster/pull/631))
- Internal: add a pyte-backed module for the upcoming live pty terminal view, with `pyte` as an optional `pty` extra kept out of the default install (no user-facing behavior yet). ([#634](https://github.com/schubydoo/clauster/pull/634))
- Internal: add the keeper-side live-screen capture (gated on `claude.pty_screen_enabled`, needs the optional `pyte` extra) that renders the bridge's terminal into a redacted, cells-only sidecar, as groundwork for the live-terminal view; no WebSocket or UI yet. ([#638](https://github.com/schubydoo/clauster/pull/638))
- Internal: add the `/ws/pty-screen` WebSocket endpoint that streams the keeper's redacted, cells-only terminal frames to the browser, as groundwork for the live-terminal view; no UI yet. ([#639](https://github.com/schubydoo/clauster/pull/639))

## 0.12.4 (2026-06-25)

[Compare with 0.12.3](https://github.com/schubydoo/clauster/compare/v0.12.3...v0.12.4)

### Fixes

- Fix the live bridge-log tail going dead after a service restart and bridge reattach, showing "Live tail disconnected" for a bridge that was actually still alive; reattached bridges now re-bind their log tail correctly, and the Reconnect button now always retries instead of becoming a no-op after auto-reconnect gives up. ([#595](https://github.com/schubydoo/clauster/pull/595))
- Fix `clauster install-service` generating an invalid service unit command for standalone-binary and console-script installs; the unit now invokes the entry point directly on systemd, launchd, and Windows/nssm. ([#588](https://github.com/schubydoo/clauster/pull/588))
- Fix `clauster install-service` so the generated systemd/launchd units bake in a usable `PATH` (`~/.local/bin` plus standard system dirs) instead of a minimal default, so spawned bridges can resolve user-local tools like `uv`/`ruff`/`pytest`; a unit comment now points operators at `claude.path_append` / `claude.env` for shell-managed toolchains (nvm/pyenv/cargo/go). ([#593](https://github.com/schubydoo/clauster/pull/593))
- Fix the hosted live-view rendering each assistant reply twice; a successful turn now collapses to a "turn complete" marker, with full text still shown on error (e.g. "Not logged in · Please run /login"). ([#596](https://github.com/schubydoo/clauster/pull/596))
- Fix clauster-spawned hosted (claustrum) sessions being misclassified as external/unmanaged, which also left a stale Active card next to the Stopped one after Stop. ([#594](https://github.com/schubydoo/clauster/pull/594))

## 0.12.3 (2026-06-24)

[Compare with 0.12.2](https://github.com/schubydoo/clauster/compare/v0.12.2...v0.12.3)

### Features

- Make the Claustrum (hosted live-view) config block editable in the config editor: the `claustrum.enabled` toggle plus its operational fields (`socket_path`, `spawn_timeout_seconds`, `keep_children`, `request_timeout_seconds`) can now be changed in-app instead of by hand-editing `clauster.yml`, with `claustrum.binary` intentionally left non-editable. ([#563](https://github.com/schubydoo/clauster/pull/563))

### Fixes

- Replace Unicode emoji with consistent Tabler icons for the tool and permission-decision badges in the hosted transcript view. ([#558](https://github.com/schubydoo/clauster/pull/558))
- Make the project name truncate at a viewport-relative width instead of a fixed cap so long names adapt to the screen, and validate that the Active-list "Open in Claude" link is an `http(s)` URL before using it. ([#546](https://github.com/schubydoo/clauster/pull/546))
- De-jargon the opt-in Maintenance panel: "Reap ghost environments" becomes "Clean up leftover environments," with plainer copy and a "Permanently delete — this can't be undone" label on the destructive action. ([#554](https://github.com/schubydoo/clauster/pull/554))
- Config editor fixes: `usage.mode` is now authoritative over the deprecated `usage.show_cost` alias, the transcript-recap toggle greys out under the pty launch mode where it has no effect, the bridge-log retention settings (`logs.retention_max_age_days`/`_max_files`/`_max_total_mb`) are now editable in-app, and a blank `usage.currency_symbol` falls back to the default instead of showing an empty badge. ([#564](https://github.com/schubydoo/clauster/pull/564))
- Security hardening: validate the reverse-proxy `trusted_ips` allowlist as IP/CIDR at load instead of silently never matching, and make the opt-in webhook SSRF guard (`webhooks.block_private_targets`) resolve DNS hostnames so a hostname pointing at an internal IP is blocked too. ([#565](https://github.com/schubydoo/clauster/pull/565))
- Config editor: the Default permission mode dropdown now shows the same friendly labels as the "Run Claude here" launch menu instead of bare enum tokens, the recap size limit greys out when transcript recap is off, and the "Where new sessions run" and "Enable metrics line" descriptions were rewritten to say what they actually do. ([#544](https://github.com/schubydoo/clauster/pull/544))
- Internal: fix a hosted-session stream-subscriber leak that could accumulate over failed spawns/reattaches and natural agent exits (no user-facing behavior change). ([#552](https://github.com/schubydoo/clauster/pull/552))
- Fix the live-tail "reconnecting…" and "disconnected" banners appearing even while the log was actively streaming. ([#538](https://github.com/schubydoo/clauster/pull/538))
- Dashboard polish: replace raw enum tokens with friendly labels across the Run button, hosted-session rows, Active-zone filter chips, and config editor dropdowns, and fix two accessibility gaps where form inputs weren't properly associated with their labels. ([#547](https://github.com/schubydoo/clauster/pull/547))
- Projects zone: the "Show all / Show fewer" toggle and 6-row cap now apply to every sort order (not just A–Z), and a project with a live session is never hidden by the cap. ([#545](https://github.com/schubydoo/clauster/pull/545))
- Fix the PTY keeper crashing on hosts with many concurrent bridges or open file descriptors (previously hit a hard 1024-descriptor ceiling). ([#557](https://github.com/schubydoo/clauster/pull/557))
- Security: fix the `sk-…` token redaction pattern to also match underscores, so an Anthropic `sk-ant-…` key could previously leak into the bridge debug log or live-tail stream unredacted. ([#551](https://github.com/schubydoo/clauster/pull/551))
- Fix a race where stopping a bridge immediately after launch could leave it running but untracked by Clauster. ([#553](https://github.com/schubydoo/clauster/pull/553))
- Dashboard: unify the two pre-launch warning vocabularies so both the header and per-project pills read as "readiness checks" instead of mixing in "preflight" jargon. ([#560](https://github.com/schubydoo/clauster/pull/560))
- Rename the confusing `claude.resume_mode` config key to `claude.launch_mode`; the old key and its `CLAUSTER_CLAUDE_RESUME_MODE` env var still work as deprecated aliases (with a warning), and `launch_mode` wins if both are set. ([#561](https://github.com/schubydoo/clauster/pull/561))

## 0.12.2 (2026-06-23)

[Compare with 0.12.1](https://github.com/schubydoo/clauster/compare/v0.12.1...v0.12.2)

### Features

- Add an optional sort control (Name / Last used / Cost) to the Projects zone — it stays in the default A–Z order until you pick a different sort, which then reveals every project ordered by recency or cost. ([#528](https://github.com/schubydoo/clauster/pull/528))
- Track bridge/session lifecycle events (spawned, ready, ended, crashed) with cost/token snapshots, powering new per-project "last used" and total-cost rollups. ([#514](https://github.com/schubydoo/clauster/pull/514))
- Add three new webhook events — `bg-settled`, `permission-needed`, and `clone-done` — each off by default; opt in per-key under `webhooks.events`. ([#518](https://github.com/schubydoo/clauster/pull/518))
- Add an opt-in SSRF guard for webhooks — set `webhooks.block_private_targets: true` to block webhook URLs that resolve to loopback, link-local, private, or other non-routable addresses (including the cloud-metadata IP) — off by default, so existing private-network receivers keep working. ([#529](https://github.com/schubydoo/clauster/pull/529))
- Add the new Clauster brand — a theme-adaptive logo lockup in the dashboard navbar, login and 404 pages, plus a refreshed favicon and the full logo kit in `assets/logo/`. ([#494](https://github.com/schubydoo/clauster/pull/494))
- Extend the bridge subprocess `PATH`/env from `clauster.yml` via `claude.path_append` and `claude.env` (both standard and pty modes), so a `claude` session can resolve user-local tools a minimal service `PATH` omits. ([#504](https://github.com/schubydoo/clauster/pull/504))

### Fixes

- Align hosted-session status badge colors with the bridge map: `starting` is azure, `stopping` is orange, and `crashed` is amber (recoverable via Resume) instead of red. ([#505](https://github.com/schubydoo/clauster/pull/505))
- Fix a transient "could not persist bridge state" warning that could appear under concurrent bridge-state writes. ([#501](https://github.com/schubydoo/clauster/pull/501))
- Fix a malformed `keeper_pid`/`bridge_pid` value (e.g. `true`/`false`) resolving to PID 1 instead of being treated as absent. ([#500](https://github.com/schubydoo/clauster/pull/500))
- Document the three `logs.retention_*` knobs in `clauster.yml.example`. ([#502](https://github.com/schubydoo/clauster/pull/502))
- Fix Forget failing to clear a dead background agent when `claude rm` soft-fails — clauster now drops the orphaned record itself once the worker is confirmed dead. ([#506](https://github.com/schubydoo/clauster/pull/506))
- Fix the live-tail "reconnecting" and "disconnected" banners overlapping, and stop the disconnect banner from claiming the bridge may have stopped while it's still running. ([#507](https://github.com/schubydoo/clauster/pull/507))
- Document the config-file search order (`$CLAUSTER_CONFIG` → `./clauster.yml` → `$CLAUSTER_HOME/clauster.yml`) in `clauster --help`. ([#522](https://github.com/schubydoo/clauster/pull/522))
- Fix a pty bridge surviving a clauster restart being orphaned as a stopped card or leaking an uncontrolled keeper process — it's now reattached as a running instance. ([#535](https://github.com/schubydoo/clauster/pull/535))

### Security

- Gate inline scripts with a per-request CSP nonce and drop `unsafe-inline` from `script-src`. ([#532](https://github.com/schubydoo/clauster/pull/532))
- Store the `/metrics` scrape token as a SHA-256 hash at rest (`observability.metrics_token_hash`), matching the API token; mint one with the new `clauster hash-metrics-token` command. ([#503](https://github.com/schubydoo/clauster/pull/503))

### Build System & Dependencies

- Internal: add a `changeset-autodraft` workflow that auto-drafts a changeset fragment on trusted PRs touching `src/`. ([#530](https://github.com/schubydoo/clauster/pull/530))

## 0.12.1 (2026-06-20)

[Compare with 0.12.0](https://github.com/schubydoo/clauster/compare/v0.12.0...v0.12.1)

### Fixes

- Harden two inputs surfaced by new fuzz tests: a malformed `Origin` header port no longer trips a 500 in the CSRF/CORS check, and a non-dict `~/.claude.json` no longer crashes project discovery. ([#492](https://github.com/schubydoo/clauster/pull/492))
- Fix toast notifications rendering ~50% transparent instead of fully opaque, so they stay readable over busy page content. ([#484](https://github.com/schubydoo/clauster/pull/484))

## 0.12.0 (2026-06-19)

[Compare with 0.11.0](https://github.com/schubydoo/clauster/compare/v0.11.0...v0.12.0)

### Features

- Add a Resume action for ended background agents. ([#336](https://github.com/schubydoo/clauster/issues/336))
- Add an API-token (Bearer) auth primitive. ([#360](https://github.com/schubydoo/clauster/issues/360))
- Harden login lockout for the reverse-proxy case. ([#358](https://github.com/schubydoo/clauster/issues/358))
- Add `clauster keepers` to stop an orphaned pty keeper that isn't attached to any card. ([#301](https://github.com/schubydoo/clauster/issues/301))
- Support `CLAUSTER_*_FILE` secret indirection for config values. ([#368](https://github.com/schubydoo/clauster/issues/368))
- Wire up the session-name-prefix and capacity bridge config knobs. ([#294](https://github.com/schubydoo/clauster/issues/294))
- Internal: lay the persistence foundation (SQLAlchemy 2.0 + Alembic) that later session-history features build on. ([#362](https://github.com/schubydoo/clauster/issues/362))
- Add a per-project preflight readiness pill. ([#335](https://github.com/schubydoo/clauster/issues/335))
- Add an in-app config editor with safe, allowlisted writes. ([#299](https://github.com/schubydoo/clauster/issues/299))
- Adopt a standard external session into a managed bridge. ([#330](https://github.com/schubydoo/clauster/issues/330))
- Add a richer display for external sessions. ([#300](https://github.com/schubydoo/clauster/issues/300))
- Gate the hosted launch mode on `CLAUSTRUM_ENABLED`. ([#375](https://github.com/schubydoo/clauster/issues/375))
- Add Homebrew/Nix installer support for macOS x86_64 and Linux arm64. ([#289](https://github.com/schubydoo/clauster/issues/289))
- Add JSON logging, enabled via the `log_format` config key. ([#361](https://github.com/schubydoo/clauster/issues/361))
- Add retention and rotation for bridge debug logs. ([#348](https://github.com/schubydoo/clauster/issues/348))
- Enrich the `/metrics` endpoint and add a scrape token. ([#352](https://github.com/schubydoo/clauster/issues/352))
- Add HTTP security-headers middleware. ([#439](https://github.com/schubydoo/clauster/issues/439))
- Show whether a session is cloud-visible (bridge) or local-only (hosted) in the UI. ([#343](https://github.com/schubydoo/clauster/issues/343))
- Add outbound lifecycle webhooks — the first extension point for integrating with other tools. ([#371](https://github.com/schubydoo/clauster/issues/371))

### Fixes

- Announce live updates to screen readers, honor reduced-motion preferences, and fix a dead-end in the Active filter. ([#384](https://github.com/schubydoo/clauster/issues/384))
- Fix toast accessibility labeling, the hosted status dot, and a double-submit race on Resume. ([#447](https://github.com/schubydoo/clauster/issues/447))
- Fix unbounded memory growth from stale login-throttle entries. ([#436](https://github.com/schubydoo/clauster/issues/436))
- Fix `clauster keepers` misclassifying or killing the wrong process by checking the keeper's command line, not just its PID. ([#470](https://github.com/schubydoo/clauster/issues/470))
- Internal: pin the atheris fuzzing dependency per Python interpreter so the 3.11 CI leg keeps a working wheel. ([#464](https://github.com/schubydoo/clauster/issues/464))
- Fix hosted live-view frames being dropped by sizing the subscriber queue to hold a full replay snapshot. ([#437](https://github.com/schubydoo/clauster/issues/437))
- Internal: correct the repo-config settings baseline (homepage URL, has_projects flag). ([#325](https://github.com/schubydoo/clauster/issues/325))
- Internal: add a drift check that verifies admin-only merge flags in the repo config. ([#328](https://github.com/schubydoo/clauster/issues/328))
- Fix bridge-state persistence not handling a disk-write failure gracefully. ([#435](https://github.com/schubydoo/clauster/issues/435))
- Fix config backup files being written with default umask permissions instead of matching the source file's mode. ([#469](https://github.com/schubydoo/clauster/issues/469))
- Apply a batch of low-severity security hardening fixes. ([#452](https://github.com/schubydoo/clauster/issues/452))
- Fix login/logout form submissions losing their Origin header by setting `Referrer-Policy: same-origin`. ([#465](https://github.com/schubydoo/clauster/issues/465))
- Fix the bridge-log live tail stranding instead of auto-reconnecting after a drop. ([#419](https://github.com/schubydoo/clauster/issues/419))
- Cap hosted live-view reconnect attempts to match the bridge-log tail's behavior. ([#444](https://github.com/schubydoo/clauster/issues/444))
- Clarify the permission-mode labels, the missing connect-URL case, and which config changes require a restart. ([#457](https://github.com/schubydoo/clauster/issues/457))
- Fix the Recent zone being hidden by the live-session filter. ([#451](https://github.com/schubydoo/clauster/issues/451))
- Fix a detached session's action button always reading "Stop" regardless of whether it's actually live. ([#332](https://github.com/schubydoo/clauster/issues/332))
- Fix the Recent group being labeled "resumable" even when it isn't. ([#334](https://github.com/schubydoo/clauster/issues/334))
- Fix action buttons getting stuck in a busy state after a failed request, blocking further clicks. ([#401](https://github.com/schubydoo/clauster/issues/401))
- Fix the hosted live-view dropping frames and failing to repaint through a reverse proxy. ([#315](https://github.com/schubydoo/clauster/issues/315))
- Show a crashed bridge's error detail directly on its card. ([#321](https://github.com/schubydoo/clauster/issues/321))

### Performance

- Cache project discovery and batch the first-paint preflight checks for a faster initial load. ([#440](https://github.com/schubydoo/clauster/issues/440))
- Serve per-bridge metrics from a server-side cache. ([#354](https://github.com/schubydoo/clauster/issues/354))
- Sample bridge metrics concurrently instead of serially. ([#407](https://github.com/schubydoo/clauster/issues/407))
- Batch the live-metrics poll through a single `/api/metrics` request. ([#438](https://github.com/schubydoo/clauster/issues/438))
- Cache the usage/transcript rollup so it's not recomputed on every request. ([#455](https://github.com/schubydoo/clauster/issues/455))
- Gzip responses and set immutable caching for versioned static assets. ([#353](https://github.com/schubydoo/clauster/issues/353))

## 0.11.0 (2026-06-15)

[Compare with 0.10.0](https://github.com/schubydoo/clauster/compare/v0.10.0...v0.11.0)

### Features

- Add one-line installers for Scoop, Homebrew, and Nix, with automatic version bumping on release. ([#287](https://github.com/schubydoo/clauster/issues/287))

## 0.10.0 (2026-06-15)

[Compare with 0.9.0](https://github.com/schubydoo/clauster/compare/v0.9.0...v0.10.0)

### Features

- Make `install-service --write` install the systemd unit directly, and give `doctor` an actionable fix for it. ([#267](https://github.com/schubydoo/clauster/issues/267))
- Add a Forget action to clear a stopped session from the Recent/resumable list. ([#268](https://github.com/schubydoo/clauster/issues/268))

### Fixes

- Fix the SessionStart recap hook failing under the frozen one-file binary. ([#279](https://github.com/schubydoo/clauster/issues/279))
- Clarify the launch Mode label and restrict the Spawn selector to the standard bridge. ([#265](https://github.com/schubydoo/clauster/issues/265))
- Fix unclickable Stop/Kill/Resume buttons on detached and hosted sessions, and correct the claude.ai framing in the UI. ([#266](https://github.com/schubydoo/clauster/issues/266))

### Performance

- Internal: shrink the hosted stop-grace period so the test suite isn't 30 seconds slower. ([#275](https://github.com/schubydoo/clauster/issues/275))

## 0.9.0 (2026-06-14)

[Compare with 0.8.0](https://github.com/schubydoo/clauster/compare/v0.8.0...v0.9.0)

### Features

- Add cloud-deregistering stop for background agent sessions. ([#218](https://github.com/schubydoo/clauster/issues/218))
- Add the ability to dispatch a `claude --bg` background session. ([#215](https://github.com/schubydoo/clauster/issues/215))
- Add a read-only background-agents panel. ([#214](https://github.com/schubydoo/clauster/issues/214))
- Wire dispatch and stop buttons into the background-agents panel. ([#220](https://github.com/schubydoo/clauster/issues/220))
- Make the usage badge configurable with currency conversion and a tokens-only mode. ([#192](https://github.com/schubydoo/clauster/issues/192))
- Add `--resume` respawn for a lost hosted session. ([#236](https://github.com/schubydoo/clauster/issues/236))
- Internal: add the claustrum daemon connect-or-spawn lifecycle that hosted sessions run on. ([#229](https://github.com/schubydoo/clauster/issues/229))
- Internal: add the claustrum NDJSON JSON-RPC client and a fake-daemon test fixture — foundation for hosted sessions. ([#224](https://github.com/schubydoo/clauster/issues/224))
- Internal: add the hosted-channel session engine. ([#230](https://github.com/schubydoo/clauster/issues/230))
- Add a live-view UI for hosted sessions. ([#233](https://github.com/schubydoo/clauster/issues/233))
- Detect and recover orphaned hosted sessions after a daemon restart. ([#237](https://github.com/schubydoo/clauster/issues/237))
- Add an approve/deny permissions UI for hosted sessions. ([#234](https://github.com/schubydoo/clauster/issues/234))
- Persist hosted sessions so they reattach automatically across a clauster restart. ([#235](https://github.com/schubydoo/clauster/issues/235))
- Internal: wire the hosted channel into the app. ([#231](https://github.com/schubydoo/clauster/issues/231))
- Redact the session URL in the on-disk bridge log. ([#200](https://github.com/schubydoo/clauster/issues/200))
- Add crash notifications via Apprise (optional extra). ([#197](https://github.com/schubydoo/clauster/issues/197))
- Add a per-project preflight endpoint (`GET /api/projects/{name}/preflight`). ([#193](https://github.com/schubydoo/clauster/issues/193))
- Default the systemd unit to `KillMode=process` so pty bridges survive a `systemctl restart`. ([#206](https://github.com/schubydoo/clauster/issues/206))
- Redesign the dashboard into two zones and migrate icons to Tabler Icons. ([#248](https://github.com/schubydoo/clauster/issues/248))

### Fixes

- Fix a stop being reported as clean when no live worker was actually found. ([#255](https://github.com/schubydoo/clauster/issues/255))
- Fix ghost WebSocket tasks stalling shutdown by ending send-only handlers on client disconnect. ([#243](https://github.com/schubydoo/clauster/issues/243))
- Enforce the bypass-permissions ceiling on hosted and background-agent channels too. ([#249](https://github.com/schubydoo/clauster/issues/249))
- Show a friendly HTML 404 page for browser navigation, and unify project-not-found wording. ([#247](https://github.com/schubydoo/clauster/issues/247))
- Ensure `session.secret` creation is durably written to disk. ([#261](https://github.com/schubydoo/clauster/issues/261))
- Internal: always run CodeQL so docs-only PRs aren't blocked by the code-scanning branch rule. ([#196](https://github.com/schubydoo/clauster/issues/196))
- Fix a batch of correctness and robustness issues found in a clean-room audit. ([#252](https://github.com/schubydoo/clauster/issues/252))
- Bump the Docker base image to clear stale OpenSSL CVEs. ([#216](https://github.com/schubydoo/clauster/issues/216))
- Fix the live metrics line rendering twice in the rows layout. ([#190](https://github.com/schubydoo/clauster/issues/190))
- Fix an oversized claustrum frame killing the hosted-session reader instead of being handled gracefully. ([#256](https://github.com/schubydoo/clauster/issues/256))
- Fix a permission-allow race with updated tool input, and a stop exit-latch race, in hosted sessions. ([#242](https://github.com/schubydoo/clauster/issues/242))
- Resolve parked permission requests on exit, and fix hosted live-view double-wiring. ([#254](https://github.com/schubydoo/clauster/issues/254))
- Fix the claustrum daemon inheriting a stray daemonize sentinel from its spawn environment. ([#241](https://github.com/schubydoo/clauster/issues/241))
- Surface a session's terminal state in the hosted live-view, and stop it endlessly reconnecting to a dead session. ([#245](https://github.com/schubydoo/clauster/issues/245))
- Fix incorrect working-directory attribution for some agent-view session kinds/states. ([#213](https://github.com/schubydoo/clauster/issues/213))
- Fix a truncated first line in the live-tail WebSocket, and write the bridge log at `0600` permissions when redaction is off. ([#259](https://github.com/schubydoo/clauster/issues/259))
- Scrub Clauster secrets from every spawned child process's environment. ([#253](https://github.com/schubydoo/clauster/issues/253))
- Harden state-file writes with restrictive permissions (`0700` dir, `0600` temp file) and fsync durability. ([#258](https://github.com/schubydoo/clauster/issues/258))
- Fix stale state lingering in the New-project dialog across close, mode-switch, and edit. ([#246](https://github.com/schubydoo/clauster/issues/246))
- Label the launch controls clearly, and guard the launch button against double-submit. ([#260](https://github.com/schubydoo/clauster/issues/260))
- Render the Active status rail and fix keyboard focus order. ([#250](https://github.com/schubydoo/clauster/issues/250))
- Restore the Tabler and Alpine.js attribution in the dashboard footer. ([#198](https://github.com/schubydoo/clauster/issues/198))
- Align status presentation across views, add an untrusted-project indicator, and gate the bypass-permissions confirmation. ([#251](https://github.com/schubydoo/clauster/issues/251))
- Fix the usage rollup failing on malformed token values instead of tolerating them. ([#257](https://github.com/schubydoo/clauster/issues/257))

## 0.8.0 (2026-06-07)

[Compare with 0.7.0](https://github.com/schubydoo/clauster/compare/v0.7.0...v0.8.0)

### Features

- Add a gated Prometheus `/metrics` endpoint. ([#178](https://github.com/schubydoo/clauster/issues/178))
- Add a read-only `/api/widget` summary endpoint. ([#179](https://github.com/schubydoo/clauster/issues/179))
- Add a cards/rows layout toggle for the dashboard. ([#173](https://github.com/schubydoo/clauster/issues/173))
- Show an honest currency label on the cost badge — the symbol alone is used only for USD. ([#167](https://github.com/schubydoo/clauster/issues/167))
- Show live per-bridge resource metrics: CPU, memory, and disk. ([#172](https://github.com/schubydoo/clauster/issues/172))

### Fixes

- Internal: stop the `@claude` CI review from cancelling itself. ([#183](https://github.com/schubydoo/clauster/issues/183))
- Correct and clarify the permission-mode tooltip. ([#165](https://github.com/schubydoo/clauster/issues/165))

## 0.7.0 (2026-06-06)

[Compare with 0.6.0](https://github.com/schubydoo/clauster/compare/v0.6.0...v0.7.0)

### Features

- Add an actionable call-to-action to the empty-state screen. ([#159](https://github.com/schubydoo/clauster/issues/159))
- Add tooltips across the dashboard card. ([#158](https://github.com/schubydoo/clauster/issues/158))

### Fixes

- Internal: address four low-severity review findings. ([#155](https://github.com/schubydoo/clauster/issues/155))
- Fix a live, clauster-launched pty bridge being misclassified as external. ([#153](https://github.com/schubydoo/clauster/issues/153))

## 0.6.0 (2026-06-05)

[Compare with 0.5.0](https://github.com/schubydoo/clauster/compare/v0.5.0...v0.6.0)

### Features

- Redesign the project card with clearer hierarchy and one primary action. ([#143](https://github.com/schubydoo/clauster/issues/143))
- Prompt to trust a directory at launch (trust-on-start). ([#144](https://github.com/schubydoo/clauster/issues/144))

### Fixes

- Suppress a false "port in use" warning in the dashboard. ([#142](https://github.com/schubydoo/clauster/issues/142))

## 0.5.0 (2026-06-05)

[Compare with 0.4.0](https://github.com/schubydoo/clauster/compare/v0.4.0...v0.5.0)

### Features

- Add `GET /api/doctor`, surfacing system readiness as JSON. ([#127](https://github.com/schubydoo/clauster/issues/127))
- Retitle the process as `clauster[<name>]` (a new `instance_name` setting) so it's identifiable in `ps`/`pgrep`. ([#130](https://github.com/schubydoo/clauster/issues/130))
- Recover the "Open session" deep link on a `--continue` resume. ([#135](https://github.com/schubydoo/clauster/issues/135))
- Distinguish "Interrupted" from "Stopped" on the dashboard card. ([#136](https://github.com/schubydoo/clauster/issues/136))
- Add a system-readiness (preflight) panel to the dashboard. ([#129](https://github.com/schubydoo/clauster/issues/129))

### Fixes

- Fix a `--continue` resume reading "Failed to start" while the bridge was actually running. ([#134](https://github.com/schubydoo/clauster/issues/134))
- Fix a phantom STOPPED instance shadowing a live external bridge. ([#133](https://github.com/schubydoo/clauster/issues/133))

## 0.4.0 (2026-06-04)

[Compare with 0.3.0](https://github.com/schubydoo/clauster/compare/v0.3.0...v0.4.0)

### Features

- Recover reboot-orphaned bridges as resumable stopped cards. ([#110](https://github.com/schubydoo/clauster/issues/110))
- Add a per-launch picker to choose standard or pty resume mode. ([#103](https://github.com/schubydoo/clauster/issues/103))
- Rename "Restart" to "Resume" and add a warned "Start new session" action. ([#101](https://github.com/schubydoo/clauster/issues/101))
- Add a `usage.show_cost` toggle to hide the cost badge. ([#121](https://github.com/schubydoo/clauster/issues/121))

### Fixes

- Fix file reads and writes to always use UTF-8 encoding instead of the platform default. ([#122](https://github.com/schubydoo/clauster/issues/122))
- Fix a bridge's resume mode to stay fixed at launch instead of following live config changes. ([#100](https://github.com/schubydoo/clauster/issues/100))
- Fix a rare race where PID reuse could cause a dead bridge to be reported as alive. ([#104](https://github.com/schubydoo/clauster/issues/104))
- Harden the transcript-recap boundary against prompt injection. ([#105](https://github.com/schubydoo/clauster/issues/105))
- Fix a race that could lose concurrent writes to `~/.claude.json`. ([#108](https://github.com/schubydoo/clauster/issues/108))

### Performance

- Internal: speed up the test suite from 48s to 14s (parallelize with xdist, cap the ready-timeout test at 15s). ([#111](https://github.com/schubydoo/clauster/issues/111))

### Build System & Dependencies

- Sign release artifacts (sdist/wheel) with Sigstore and attach them to each GitHub Release. ([#114](https://github.com/schubydoo/clauster/issues/114))
- Internal: adopt CodeRabbit as the automatic PR reviewer, with `@claude` as an on-demand backup. ([#120](https://github.com/schubydoo/clauster/issues/120))
- Internal: move the Trivy image scan to main-push and cron instead of running on every PR. ([#112](https://github.com/schubydoo/clauster/issues/112))
- Internal: tune Codecov configuration to best practice. ([#115](https://github.com/schubydoo/clauster/issues/115))
- Internal: skip the coverage upload on release-please PRs. ([#109](https://github.com/schubydoo/clauster/issues/109))
- Internal: add an end-to-end test for the clone pipeline. ([#106](https://github.com/schubydoo/clauster/issues/106))
- Internal: add Windows pty-mode guard test coverage. ([#107](https://github.com/schubydoo/clauster/issues/107))

## 0.3.0 (2026-06-03)

[Compare with 0.2.2](https://github.com/schubydoo/clauster/compare/v0.2.2...v0.3.0)

### Features

- Add a Docker Compose quickstart. ([#97](https://github.com/schubydoo/clauster/issues/97))
- `clauster doctor` now checks that the `claude` CLI is logged in. ([#84](https://github.com/schubydoo/clauster/issues/84))

### Fixes

- Fix a race that let concurrent spawns of the same project collide. ([#91](https://github.com/schubydoo/clauster/issues/91))
- Fix persisted metadata for untracked projects being wiped. ([#92](https://github.com/schubydoo/clauster/issues/92))
- Show the Restart action for stopped pty bridges so true-resume is reachable. ([#99](https://github.com/schubydoo/clauster/issues/99))

## 0.2.2 (2026-06-03)

[Compare with 0.2.1](https://github.com/schubydoo/clauster/compare/v0.2.1...v0.2.2)

### Fixes

- Refuse to start on a non-loopback bind unless auth is actually enforced. ([#88](https://github.com/schubydoo/clauster/issues/88))

### Security

- This is a security release. Binding to a non-loopback address (e.g. `0.0.0.0` or a LAN IP) could serve the dashboard unauthenticated — even with a password configured — because `auth.enabled` defaulted to `false` and was not required for a network bind. Clauster now refuses to start on a non-loopback bind unless auth is actually enforced. All prior releases (≤0.2.1), including the Docker image, are affected. Upgrade, and set `auth.enabled: true` on any networked deployment. See [GHSA-h4g2-xfmw-q2c9](https://github.com/schubydoo/clauster/security/advisories/GHSA-h4g2-xfmw-q2c9).

## 0.2.1 (2026-06-03)

[Compare with 0.2.0](https://github.com/schubydoo/clauster/compare/v0.2.0...v0.2.1)

### Fixes

- Fix README images not rendering on PyPI by using absolute GitHub URLs. ([#79](https://github.com/schubydoo/clauster/issues/79))

## 0.2.0 (2026-06-03)

### Features

- Add the authentication foundation: password login, WebSocket auth, reverse-proxy trust, and `state.json` persistence. ([b9f40eb](https://github.com/schubydoo/clauster/commit/b9f40eb4081bfd28e0c4eeaf7750840db213e0ae))
- Add a CLAUDE.md viewer/editor. ([4bb7a6e](https://github.com/schubydoo/clauster/commit/4bb7a6e23e0da8aed70ec36d56f2ebf3513cc7a8))
- Add async project cloning with live progress over WebSocket. ([#52](https://github.com/schubydoo/clauster/issues/52))
- Add cost/token tracking from session transcripts. ([842e6dc](https://github.com/schubydoo/clauster/commit/842e6dc831331353a88358161ed598ef976fe51f))
- Ship a multi-arch GHCR Docker image with an integrated Trivy vulnerability scan. ([#14](https://github.com/schubydoo/clauster/issues/14))
- `clauster doctor` now warns when a source checkout is behind upstream. ([#34](https://github.com/schubydoo/clauster/issues/34))
- Add an opt-in ghost-environment reaper UI to the dashboard. ([15d50e5](https://github.com/schubydoo/clauster/commit/15d50e5a75a0baf1f15be938b61859f1825aa5cf))
- Add a ghost-environment reaper, dry-run by default. ([5a5fadd](https://github.com/schubydoo/clauster/commit/5a5fadd496d9d5d186394cd18e6fdd568f9aa081))
- Internal: add a docstring-coverage lint gate (pydocstyle) and backfill missing docstrings. ([#42](https://github.com/schubydoo/clauster/issues/42))
- Add packaging/ops CLIs (`doctor`, `backup`, `restore`, `migrate`, `install-service`) and a PyInstaller build. ([b13f5e9](https://github.com/schubydoo/clauster/commit/b13f5e99d8861b253316a9fa7ac5bc49b5f0f36f))
- Add a per-project cost badge to the dashboard. ([7d67f94](https://github.com/schubydoo/clauster/commit/7d67f94ef86fe9d9375004e007e7f46869349c16))
- Add project create and clone. ([599c57b](https://github.com/schubydoo/clauster/commit/599c57b2127fc3e00a8beb8b2da256dcace09cd5))
- Add project discovery and the initial dashboard. ([54591cc](https://github.com/schubydoo/clauster/commit/54591cc8c4ea846e35b321787f61bac832d0d433))
- Add real logout revocation via a server-held session epoch. ([d0c37a5](https://github.com/schubydoo/clauster/commit/d0c37a5bfbe8be4c555b8ba6999a9c27f1600c86))
- Recap the prior conversation into a restarted bridge (opt-in). ([#39](https://github.com/schubydoo/clauster/issues/39))
- Resume stopped bridges and surface bridge startup errors. ([#36](https://github.com/schubydoo/clauster/issues/36))
- Add PTY true-resume mode (backend). ([#58](https://github.com/schubydoo/clauster/issues/58))
- Auto-enable remote control so bridges skip the y/n prompt. ([#29](https://github.com/schubydoo/clauster/issues/29))
- Add graceful stop on Windows via `CTRL_BREAK`. ([#13](https://github.com/schubydoo/clauster/issues/13))
- Add bridge spawn/stop, cross-checked against `claude agents --json`. ([71a5965](https://github.com/schubydoo/clauster/commit/71a5965e8c1e11b24dcbc36d6f4fb8a9632a67b2))
- Add spawn-mode and permission-mode pickers, gated to prevent risky combinations. ([02c1da8](https://github.com/schubydoo/clauster/commit/02c1da861b793528d39b76367777c08174bc0cd3))
- Show a connection-lost banner and inline action errors instead of failing silently. ([#56](https://github.com/schubydoo/clauster/issues/56))
- Insert new project cards reactively instead of reloading the page. ([#55](https://github.com/schubydoo/clauster/issues/55))
- Show a live clone progress bar with visible errors. ([#53](https://github.com/schubydoo/clauster/issues/53))
- Rebuild the dashboard and login page on Tabler, with dark/light theme support. ([#40](https://github.com/schubydoo/clauster/issues/40))
- Add a true-resume badge and recover the keeper on pty rediscovery. ([#76](https://github.com/schubydoo/clauster/issues/76))
- Add Iconoir icons to dashboard actions and the theme toggle. ([#57](https://github.com/schubydoo/clauster/issues/57))
- Show the session URL and a QR code for sessions. ([d1323c4](https://github.com/schubydoo/clauster/commit/d1323c43cd27d43202f7135294cd3baafdb61f8f))
- Stream a redacted bridge-log tail over WebSocket. ([5151fea](https://github.com/schubydoo/clauster/commit/5151fea817f2226ae336c13010f6776882542295))

### Fixes

- Fix four UI bugs found in live testing. ([ce99b3e](https://github.com/schubydoo/clauster/commit/ce99b3e4b981c632376e285b058e6277fd7fa97c))
- Internal: address multi-agent review findings (type/config hardening + tests; no behavior change). ([39c6a43](https://github.com/schubydoo/clauster/commit/39c6a43b739844e7118e9441b9891adf318abb3c))
- Fix the session-epoch bump so it can't regress below the in-memory value. ([#25](https://github.com/schubydoo/clauster/issues/25))
- Fix two deferred review items: a backup error and an insecure-cookie warning. ([fd8bcd6](https://github.com/schubydoo/clauster/commit/fd8bcd669ce2d2ef86644406ee3c34a6a810b458))
- Fix restore to be atomic, correct IPv6 origin handling, and bound pagination. ([#30](https://github.com/schubydoo/clauster/issues/30))
- Mask bare UUIDs (organization ID, bridge ID) in the WebSocket log stream. ([#51](https://github.com/schubydoo/clauster/issues/51))
- Internal: fix Renovate matching for vendored `versions.txt` (glob instead of path-anchored regex). ([#48](https://github.com/schubydoo/clauster/issues/48))
- Internal: stop Renovate from ignoring `src/clauster/static/vendor` via its default ignore paths. ([#49](https://github.com/schubydoo/clauster/issues/49))
- Resolve executable paths and harden bridge spawn/stop. ([#17](https://github.com/schubydoo/clauster/issues/17))
- Fix a slow-but-alive bridge being marked ERROR instead of STARTING. ([#27](https://github.com/schubydoo/clauster/issues/27))
- Require environment registration before reporting a bridge RUNNING. ([#28](https://github.com/schubydoo/clauster/issues/28))
- Tolerate an unparseable process-start pointer during bridge rediscovery. ([#23](https://github.com/schubydoo/clauster/issues/23))
- Trust-gate CLAUDE.md and harden CSRF, throttling, secrets, and backups. ([#18](https://github.com/schubydoo/clauster/issues/18))
- Relabel "Resume" to "Restart" since it doesn't restore the conversation. ([#38](https://github.com/schubydoo/clauster/issues/38))
- Tolerate invalid UTF-8 bytes when parsing transcripts. ([#22](https://github.com/schubydoo/clauster/issues/22))

### Build System & Dependencies

- Internal: sync `uv.lock` with `pyproject.toml` (drop the logfire dependency tree, add ruff and pyright). ([48abfcd](https://github.com/schubydoo/clauster/commit/48abfcdba851dee46ab5e367f98a3ea19f6af918))
