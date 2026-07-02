# Changelog

## 0.12.9 (2026-06-29)

[Compare with 0.12.8](https://github.com/schubydoo/clauster/compare/v0.12.8...v0.12.9)

### Features

- Confirm before cancelling an in-progress clone, and reattach a second tab to a live clone's progress instead of showing nothing ([#708](https://github.com/schubydoo/clauster/pull/708))
- Make the application `log_format` editable in the in-app config editor and add a coverage guard that asserts every config field is a deliberate editable-or-excluded decision. ([#705](https://github.com/schubydoo/clauster/pull/705))
- Add a gated `config-write` surface for `settings.json` `hooks` (project + user scope) behind the off-by-default fail-closed Foundation gate; the structural validator only checks shape (recognized event, string matcher, `type: "command"`, non-empty command, optional int timeout) and never resolves, parses, or runs a hook command (#690). ([#740](https://github.com/schubydoo/clauster/pull/740))
- Add a gated `config_write` API for MCP servers behind the fail-closed gate: a structural-only (validate-never-execute) validator + router for project `.mcp.json` and user `mcpServers`, with type-the-name confirm, path containment, stale-hash guard, and secret redaction inherited from the #347 foundation — no dashboard UI yet (#688) ([#732](https://github.com/schubydoo/clauster/pull/732))
- Add a gated `config_write` API for permission rules (`settings.json` `permissions` allow/deny/defaultMode) behind the fail-closed gate: a structural-only (validate-never-execute) validator + router for project `.claude/settings.json` and user `~/.claude/settings.json`, with type-the-name confirm, path containment, stale-hash guard, and `bypassPermissions` kept behind the existing footgun gate — no dashboard UI yet (#689) ([#738](https://github.com/schubydoo/clauster/pull/738))
- Add the fail-closed `config_write` trust-tier foundation (off-by-default capability + scope gate, type-the-name confirm, structural secret redaction, and a shared `~/.claude.json` lock+merge+atomic writer factored out of trust.py) ([#706](https://github.com/schubydoo/clauster/pull/706))
- Add a `clauster mcp` read-only stdio MCP server exposing `list_sessions` and `session_status` tools that report Clauster's bridge, hosted, background-agent, and external sessions to any MCP client ([#710](https://github.com/schubydoo/clauster/pull/710))
- Add an opt-in `CLAUSTER_PYTE_PATH` env var so a standalone-binary user can enable the read-only live terminal view by separately installing `pyte` and pointing it at that directory, without bundling LGPL code ([#702](https://github.com/schubydoo/clauster/pull/702))
- Live-tail a running session's transcript in the read-only viewer: a new offset-based tail endpoint polls the `.jsonl` from the last byte position and appends redacted new turns as the agent works ([#709](https://github.com/schubydoo/clauster/pull/709))

### Fixes

- Drop `'unsafe-inline'` from the CSP `style-src` by nonce-gating the inline `<style>` blocks and lifting every inline `style=""` attribute into a CSS class (#533). ([#635](https://github.com/schubydoo/clauster/pull/635))
- Fix pty bridges intermittently showing "No web link — use Logs": the keeper now scrapes the connect URL from the pyte-reassembled screen (the live-view winsize makes claude fragment it with cursor-positioning escapes the raw scan can't follow), so "Open in Claude" surfaces reliably (#665). ([#721](https://github.com/schubydoo/clauster/pull/721))
- Unify the launch + permission mode labels into one server-injected canonical map (`{mode: {short, long, effect}}`), so the launch picker, the inline JS helpers, and the config editor all read a single source instead of three hand-maintained copies (#685). ([#729](https://github.com/schubydoo/clauster/pull/729))
- Reshape the launch flow into a single-screen popover: the Mode selector and Spawn fold under one "More options" disclosure (tucked away by default), the trust-on-start and bypassPermissions confirm gates render inside the popover instead of as below-the-row alerts, and the default launch mode stays the safe Desktop choice rather than auto-flipping to the experimental browser channel. ([#731](https://github.com/schubydoo/clauster/pull/731))
- Fix the launch popover (#686): a failed trust-on-start now closes the popover so the error is visible, instead of leaving it hidden behind the open card. ([#739](https://github.com/schubydoo/clauster/pull/739))
- Add a dismissible first-run orientation card to the empty dashboard explaining what Clauster is and how to start, with the dismissal persisted in localStorage (#692). ([#728](https://github.com/schubydoo/clauster/pull/728))
- Fix a freshly-spawned bridge's auto-created session briefly reading as an EXTERNAL/unmanaged "phantom" row during the bridge's Starting window — reconcile now attributes a STARTING bridge's cwd, not only a live one (#713) ([#714](https://github.com/schubydoo/clauster/pull/714))
- Harden `atomic_write_text`: close the raw `mkstemp` fd if `os.fdopen` fails (an EMFILE/ENFILE leak the temp-only cleanup missed), and cover the write/fsync-failure and interrupt-mid-write cleanup paths with tests. ([#718](https://github.com/schubydoo/clauster/pull/718))
- Hide a deprecated config-editor field once its key is removed from disk, and stop interactive `config reconcile` from offering to override an already-set replacement ([#703](https://github.com/schubydoo/clauster/pull/703))
- Apply dynamic dashboard styles via object-form Alpine `:style` so the strict nonce-gated `style-src` (no `unsafe-inline`) no longer blocks the progress-bar fill and the non-name sort layout. ([#742](https://github.com/schubydoo/clauster/pull/742))
- Stamp the per-request CSP nonce on xterm.js's runtime-injected `<style>` elements so the read-only live terminal renders under the strict nonce-gated `style-src` (no `unsafe-inline`). ([#743](https://github.com/schubydoo/clauster/pull/743))
- Docs: the `pty_screen_enabled` reference now documents the `CLAUSTER_PYTE_PATH` side-load on the standalone binary (#702) instead of claiming the live view is impossible there, and the install guide lists `clauster hash-metrics-token` + `clauster config reconcile`. ([#717](https://github.com/schubydoo/clauster/pull/717))
- Fix a transient mis-render in the hosted live-event stream: key the events `x-for` on a stable per-item id instead of the list index, so the `MAX_LOG_LINES` front-splice no longer rebinds rows to the wrong events past 1000 lines. ([#720](https://github.com/schubydoo/clauster/pull/720))
- Fix a `HostedManager.stop()` KeyError (an unmapped 500) when a concurrent forget/resume pops the hosted session's registry row during the stop grace window — re-fetch after the await and surface a clean 404 instead. ([#715](https://github.com/schubydoo/clauster/pull/715))
- Fix `clauster keepers`: read the carded-project set from the DB-backed store, not the flat `state.json` (renamed `*.imported` after the JSON→DB migration) — otherwise every live keeper was mislabeled an orphan and `--kill` could reap a carded, dashboard-managed keeper. ([#719](https://github.com/schubydoo/clauster/pull/719))
- Make the live-terminal "pyte unavailable" error honest on the standalone binary — pyte is LGPL and not bundled, so point binary users at a `pip`/`uv` install with the `[pty]` extra instead of the dead-end `install clauster[pty]`; documented in installation/configuration and the in-app editor help ([#700](https://github.com/schubydoo/clauster/pull/700))
- Collapse the `/api/projects/sortmeta` N+1: a batched `sortmeta_for_all` (two grouped queries in one session) replaces the per-project 3-SELECT rollup loop on the dashboard sort path. ([#716](https://github.com/schubydoo/clauster/pull/716))
- Swap structural Unicode emoji (warnings, carets, refresh, pause, back arrow, check/x) in the dashboard for consistent inline Tabler symbol icons ([#704](https://github.com/schubydoo/clauster/pull/704))

## 0.12.8 (2026-06-28)

[Compare with 0.12.7](https://github.com/schubydoo/clauster/compare/v0.12.7...v0.12.8)

### Features

- Add a dedicated Cancel button to the in-progress clone flow plus a confirmation toast when a clone is cancelled ([#684](https://github.com/schubydoo/clauster/pull/684))
- Add an optional `tls` config block so Clauster can terminate HTTPS natively from an existing cert + key (uvicorn `ssl_certfile`/`ssl_keyfile`), validated fail-closed at load and at server start, and warn (non-fatally) when the private key is group/other-readable. ([#695](https://github.com/schubydoo/clauster/pull/695))

### Fixes

- Correct accuracy drift in the public docs (changeset-bot flow, Tier-A editable allowlist, restart/KillMode survival, doctor warnings, bridge-log glob, example config) and add a drift guard for the editable-fields table. ([#683](https://github.com/schubydoo/clauster/pull/683))
- Fix Interactive (PTY) sessions failing to launch under the standalone binary — spawn the keeper via a frozen-binary subcommand instead of the `python -m` form that the binary's CLI rejects ([#697](https://github.com/schubydoo/clauster/pull/697))
- Adopt Anthropic's Remote Control vocabulary (Server Mode / Interactive Session / Background Agent / Direct Session) across the UI and docs. ([#681](https://github.com/schubydoo/clauster/pull/681))

## 0.12.7 (2026-06-27)

[Compare with 0.12.6](https://github.com/schubydoo/clauster/compare/v0.12.6...v0.12.7)

### Features

- Add an in-app "Restart Clauster" action to the config editor that re-execs the process in place (`os.execv`, same PID, reloads config) so a saved config change can be applied without dropping to a shell; gated behind the existing restart-impact confirmation and exposed via an auth-gated `POST /api/restart`. ([#637](https://github.com/schubydoo/clauster/pull/637))
- Add a browser-notifications channel, split notifications into per-channel and per-event toggles, and emit ready/stop/permission-needed/session-ended/reconnect-failed events. ([#636](https://github.com/schubydoo/clauster/pull/636))
- Add `clauster config reconcile`, an interactive CLI that removes deprecated config keys and writes their replacements via the atomic config writer. ([#650](https://github.com/schubydoo/clauster/pull/650))
- Add a server-side cancel for an in-progress clone so the UI abort actually stops the git transfer ([#633](https://github.com/schubydoo/clauster/pull/633))
- Badge a transcript as live in the read-only viewer when its session id maps to a currently-running bridge, agent, or hosted session. ([#653](https://github.com/schubydoo/clauster/pull/653))
- Scale the read-only live terminal to fit the panel width on narrow viewports via a client-side CSS transform (no wire-geometry change). ([#654](https://github.com/schubydoo/clauster/pull/654))

### Fixes

- Quickstart gains a copy-paste Claude Code install + `claude login` step, and the doctor version-FAIL message now suggests `claude update`. ([#649](https://github.com/schubydoo/clauster/pull/649))
- Fix the project-sort cap: changing sort no longer flashes the full list, and returning to A–Z restores the 6-row cap and "Show all N" toggle. ([#655](https://github.com/schubydoo/clauster/pull/655))
- Correct the Claude Code install steps (native installer; npm is deprecated) and the clone dialog's helper text (name the `clone.allow_private_hosts` clauster.yml key; clarify cloning only fetches files). ([#658](https://github.com/schubydoo/clauster/pull/658))
- Project sort: selecting a non-name sort (Last used / Cost) no longer uncaps the list past the 6-row limit. ([#661](https://github.com/schubydoo/clauster/pull/661))
- Lower the default `claude.agents_json_poll_interval_seconds` from 300 to 30 so session liveness (the transcript live badge, active-session zone) and crash detection refresh within ~30s instead of up to 5 minutes. ([#662](https://github.com/schubydoo/clauster/pull/662))
- Fix the in-app Restart: the page reloads once the server is back (no more stuck "Restarting…"), and the confirmation now correctly says running sessions survive the restart instead of warning they end. ([#666](https://github.com/schubydoo/clauster/pull/666))
- Browser notifications now prompt for permission the moment you enable the channel instead of only after a reload, and a failed bridge resume only raises the "reconnect failed" notification when the bridge genuinely could not restart (not when the session was already gone or the request was invalid). ([#668](https://github.com/schubydoo/clauster/pull/668))
- The config editor now flags when browser notifications can't be delivered in your browser/connection (insecure non-HTTPS context, unsupported browser, or blocked permission) and disables the toggle when it can't work, instead of silently offering a setting that does nothing. Browser notifications require a secure context (HTTPS or localhost). ([#675](https://github.com/schubydoo/clauster/pull/675))
- `clauster config reconcile --dry-run` is now non-interactive — it previously ran the per-key prompt before the dry-run guard and blocked on a terminal; it now prints the plan and writes nothing without prompting. ([#667](https://github.com/schubydoo/clauster/pull/667))

## 0.12.6 (2026-06-27)

[Compare with 0.12.5](https://github.com/schubydoo/clauster/compare/v0.12.5...v0.12.6)

### Fixes

- Fix the in-app config editor's enum dropdowns (Launch mode, Usage badge mode, and every other `<select>`) showing the first option instead of the saved value — `x-model` ran before its `x-for` options existed, so the browser fell back to option index 0 (e.g. displaying "Standard" while the bridge default was `pty`, or "Cost" while the badge was `off`), which also left Save greyed when you re-picked the real value; each option now binds `:selected` to the model value so the dropdown reflects what is actually on disk. ([#644](https://github.com/schubydoo/clauster/pull/644))
- Make the in-app config editor reflect the current on-disk config: `GET /api/config` now reads the editable field values from the file (consistent with the content hash) instead of the startup config captured in memory. A save writes the file but deliberately does not live-reload the running config, so previously reopening the editor after a save showed the stale pre-save values until a restart — making a successful save look reverted. The runtime still only adopts the change on restart (the `restart_required` flag is unchanged); an unreadable/corrupt file falls back to the in-memory values so the editor still opens. ([#645](https://github.com/schubydoo/clauster/pull/645))
- Fix the dashboard's per-bridge live-session count (the #570/#622 expander) never appearing for a `spawn_mode: worktree` bridge. `claude remote-control --spawn worktree` runs each session in a per-session git worktree under `<project>/.claude/worktrees/`, but session→bridge attribution joined only on an exact project-root cwd match, so a worktree session read as EXTERNAL instead of TRACKED and was excluded from the count (and from the bridge's tracked-session liveness). Attribution now also matches a worktree-spawn bridge's `.claude/worktrees` subtree by containment (most-specific root first, so a nested project's bridge wins), while same-dir/session bridges keep the exact-cwd join. ([#646](https://github.com/schubydoo/clauster/pull/646))

## 0.12.5 (2026-06-27)

[Compare with 0.12.4](https://github.com/schubydoo/clauster/compare/v0.12.4...v0.12.5)

### Features

- Add an opt-in trusted-header (forward-auth) reverse-proxy mode (`auth.reverse_proxy.require_hmac: false`) so SSO proxies that don't sign a per-request HMAC (Authelia, authentik, Caddy, Traefik, oauth2-proxy) authenticate via `trusted_ips` + `user_header` alone; both modes now require `trusted_ips`, and a `reverse_proxy.enabled` config without it fails fast at startup instead of silently authenticating no one. ([#617](https://github.com/schubydoo/clauster/pull/617))
- Add a read-only in-dashboard transcript viewer: a per-project "View transcript" button opens a modal listing each session's `.jsonl` file (newest-first, with turn counts) and renders turns with cursor-based pagination; content passes through `redact.sanitize_line` and renders via Alpine `x-text` only. ([#611](https://github.com/schubydoo/clauster/pull/611))
- Add the read-only live PTY terminal view (#534, completes the epic): when `claude.pty_screen_enabled` is on, each pty bridge gets a "Live terminal" button that streams the keeper's redacted, cells-only screen frames over `/ws/pty-screen` into an xterm.js terminal (auth-gated, never raw ANSI); the flag is now toggleable in the in-app config editor. ([#640](https://github.com/schubydoo/clauster/pull/640))
- List every live session under a standard `claude remote-control` bridge: an Active-session card now expands to enumerate each working session with a short-UUID label, uptime, and a per-session deep link into the Claude web app. ([#618](https://github.com/schubydoo/clauster/pull/618))
- Add an in-app help page: a `?` icon in every page's navbar opens a keyboard-accessible offcanvas covering launch modes, permission modes, the dashboard zones, and session types, with a link to the README. ([#619](https://github.com/schubydoo/clauster/pull/619))
- When a non-git project's default spawn mode is `worktree`, the "Run Claude here" popover now falls back to `same-dir` and shows a note rather than letting the server reject the spawn, keeping the picker and trust-on-start confirm reachable. ([#615](https://github.com/schubydoo/clauster/pull/615))
- Add a sort-direction toggle (newest-first / oldest-first) and an in-message search box to the transcript viewer; search filters turns by substring against already-redacted content, so it can never confirm a masked secret. ([#616](https://github.com/schubydoo/clauster/pull/616))
- Add an `instance_defaults.verbose` config toggle (default off, editable from the in-app config editor) that passes `--verbose` to spawned standard `claude remote-control` bridges in every spawn mode (same-dir/worktree/session) for detailed connection/session logs; the pty (flag-form) bridge is intentionally never passed `--verbose` so its live-screen tap stays clean. ([#642](https://github.com/schubydoo/clauster/pull/642))

### Fixes

- Rename the detached/background session type to "Background" consistently across the launch-picker, filter chip, and card badge (previously "Fire-and-forget" / `detached`), so one session type reads as one name everywhere (UX-04). ([#567](https://github.com/schubydoo/clauster/pull/567))
- Show the live-session list from the first live session on a standard bridge (not only at two or more), giving each session its own `claude.ai/code` deep link; the toggle label is now singular/plural-aware. ([#622](https://github.com/schubydoo/clauster/pull/622))
- Two dashboard error-UX fixes: the default-session Stop button now asks for confirmation (matching every other destructive action), and error toasts now persist until dismissed instead of auto-vanishing after 4.5 s. ([#627](https://github.com/schubydoo/clauster/pull/627))
- Fix the config-editor "restart Clauster to apply" help link, which pointed at a nonexistent README anchor; it now targets the Operations runbook's restart section, which carries a stable custom anchor to prevent future rot. ([#624](https://github.com/schubydoo/clauster/pull/624))
- `clauster.yml.example` now uses `projects_root: ~/code` (matching every prose doc) instead of `/srv/projects`, which contradicted the docs and hard-failed validation if copied as-is on a box without that directory. ([#625](https://github.com/schubydoo/clauster/pull/625))
- Correct the `webhooks.block_private_targets` config-field description: it claimed DNS hostnames are not resolved, but the opt-in SSRF guard does resolve them at filter time — documentation only, behaviour unchanged. ([#598](https://github.com/schubydoo/clauster/pull/598))
- a11y: the clone-progress bar now exposes `aria-valuemin`, `aria-valuemax`, `aria-valuenow`, and `aria-valuetext` so assistive technology can announce progress (previously `role="progressbar"` had no value attributes). ([#607](https://github.com/schubydoo/clauster/pull/607))
- The "Recap prior transcript on restart" config toggle is now always editable instead of greyed out when the default launch mode isn't `standard`; an informational note explains it applies to standard bridges only. ([#600](https://github.com/schubydoo/clauster/pull/600))
- Align the README "First bridge in 60 seconds" walkthrough with the shipped UI: "Run Claude here" replaces the removed "Start" button, "Open in Claude" fixes the link label, and the dead "Start new session" step is replaced with the actual forget-and-relaunch path. ([#605](https://github.com/schubydoo/clauster/pull/605))

### Security

- Redact a failed clone's `error_detail` (git stderr tail) on the progress WebSocket, closing a redaction asymmetry with the clone-done webhook path where the same value was already redacted. ([#606](https://github.com/schubydoo/clauster/pull/606))

### Build System & Dependencies

- Vendor xterm.js 6.0.0 (self-hosted under `static/vendor/xterm/`, Renovate-pinned) as the front-end foundation for the read-only live pty terminal view (#534), and document the front-end vendoring convention in CONTRIBUTING. No user-facing behavior yet — the terminal view, its WebSocket, and the (default-off) config flag land in later slices. ([#631](https://github.com/schubydoo/clauster/pull/631))
- Add a pyte-backed module that renders redacted, cells-only terminal frames for the read-only live pty terminal view (#534), with pyte as an optional LGPL `pty` extra (lazy-imported, kept out of the default install and binary) — groundwork, no user-facing behavior yet. ([#634](https://github.com/schubydoo/clauster/pull/634))
- Add the keeper-side live-screen tap (#534): when `claude.pty_screen_enabled` is on (default off, needs the optional `pyte` extra), the PTY keeper renders the bridge's terminal into a redacted, cells-only screen sidecar — strictly best-effort, never affecting the bridge — as groundwork for the live-terminal view; no WebSocket or UI yet. ([#638](https://github.com/schubydoo/clauster/pull/638))
- Add the `/ws/pty-screen` WebSocket endpoint (#534): it polls the keeper's redacted screen sidecar and streams cells-only frames (de-duped by seq, never raw ANSI) to the browser, gated on a pty bridge with `claude.pty_screen_enabled` on; groundwork for the live-terminal view — no UI yet. ([#639](https://github.com/schubydoo/clauster/pull/639))

## 0.12.4 (2026-06-25)

[Compare with 0.12.3](https://github.com/schubydoo/clauster/compare/v0.12.3...v0.12.4)

### Fixes

- Fix the live bridge-log tail going dead after a service restart + bridge reattach (the upgrade path). A reattached bridge was rebuilt without its `bridge_debug_log_path`, so `/ws/bridge-log/{instance_id}` closed every connection immediately — the tail flickered through a few reconnect attempts and then gave up with "Live tail disconnected", leaving the operator blind to a bridge that was actually alive. Both reattach paths now re-bind the tail to the log the bridge is still writing: a pty survivor derives it from its keeper sidecar's shared spawn-set stem, and a standard survivor recovers the newest debug log it wrote. The Reconnect button also resets the consecutive-failure counter on a manual retry so it can no longer be a no-op once auto-reconnect has capped out. ([#595](https://github.com/schubydoo/clauster/pull/595))
- Fix `clauster install-service` so a frozen/standalone binary (or `clauster` console-script) install no longer emits a service unit with an invalid `clauster -m clauster run` command — the unit now invokes the clauster entry point directly across systemd, launchd, and Windows/nssm, and only a bare `python -m clauster` interpreter keeps the module prefix. ([#588](https://github.com/schubydoo/clauster/pull/588))
- Fix `clauster install-service` so the generated systemd and launchd units bake a `PATH` (the run-as user's `~/.local/bin` plus the standard system dirs) instead of leaving the service with a minimal default. Clauster propagates its environment to every spawned bridge, so previously a bridge agent couldn't resolve user-local tools (`uv`/`ruff`/`pytest`, etc.) that work fine in an interactive shell. A unit comment now points operators at `claude.path_append` / `claude.env` for shell-managed toolchains (nvm/pyenv/cargo/go). ([#593](https://github.com/schubydoo/clauster/pull/593))
- Fix the hosted live-view rendering each assistant reply twice. One assistant turn emits both the streamed `assistant` frame and a trailing `result` frame whose `result` field repeats the same text, and the live-view rendered both — the message as white paragraphs and the result echo as a green run-on block. The `result` frame now collapses a successful turn to a "turn complete" marker and surfaces text only on the error path (where `result` carries content no assistant frame emits, e.g. "Not logged in · Please run /login"). ([#596](https://github.com/schubydoo/clauster/pull/596))
- Fix clauster-spawned hosted (claustrum) sessions being misclassified as `EXTERNAL`/"unmanaged" by the `claude agents --json` cross-check, which also left a stale Active card alongside the Stopped one after Stop — the poll loop now recognizes the hosted registry (by claustrum agent pid, with a workspace-cwd fallback for pre-CT-1 daemons, plus CL-8 orphan survivors) and attributes those sessions to Clauster instead of surfacing them as external. ([#594](https://github.com/schubydoo/clauster/pull/594))

## 0.12.3 (2026-06-24)

[Compare with 0.12.2](https://github.com/schubydoo/clauster/compare/v0.12.2...v0.12.3)

### Features

#### Config editor: the **Claustrum (hosted live-view)** block is now editable in-app (#539). ([#563](https://github.com/schubydoo/clauster/pull/563))

Previously `claustrum.enabled` could only be flipped by hand-editing `clauster.yml` and
restarting, so a user with claustrum installed had no discoverable way to turn the hosted
channel on. The editor now surfaces the `claustrum.enabled` master toggle plus its
operational fields (`socket_path`, `spawn_timeout_seconds`, `keep_children`,
`request_timeout_seconds`), which grey out when the channel is off (same depends-on
mechanism as the metrics block). Like every config edit, saving prompts to restart Clauster
to apply. `claustrum.binary` is intentionally left out — executable paths stay non-editable,
matching `claude.binary`.

### Fixes

- Dashboard (hosted live view): the tool and permission-decision badges in the hosted transcript now use Tabler sprite icons instead of Unicode emoji (🔧 → `tool`, ✓/✕ → `check`/`x`), matching the sprite-icon system the rest of the UI uses. ([#558](https://github.com/schubydoo/clauster/pull/558))
- Dashboard polish (#433): the project name now truncates at a **viewport-relative** width (≈10rem on a phone up to 28rem on a wide screen) instead of a fixed 16rem cap, so long names adapt to the screen (DES-07). The Active-list "Open in Claude" link now validates that the session URL is `http(s)` before binding it to the `href`, so a non-http value can never reach the link (FE-03 hardening). ([#546](https://github.com/schubydoo/clauster/pull/546))
- Dashboard: de-jargon the (opt-in) Maintenance panel. "Reap ghost environments" → "Clean up leftover environments", with plainer copy explaining that these are cloud session environments left over after a session ends (the cloud `Default` and any environment still backing a live session are never touched), and the destructive action labelled "Permanently delete — this can't be undone" (the typed-`DELETE` confirm is unchanged). ([#554](https://github.com/schubydoo/clauster/pull/554))
- Config editor: several correctness + UX papercuts. The usage badge mode (`usage.mode`) is now authoritative over the deprecated `usage.show_cost` alias — `show_cost` is flagged deprecated in the panel (with a plain-language note pointing at `usage.mode`) instead of leaking its raw docstring, and an explicit `usage.mode` wins if both are set. The transcript-recap toggle now greys out under the pty (true-resume) launch mode, where it has no effect. The bridge-log retention knobs (`logs.retention_max_age_days` / `_max_files` / `_max_total_mb`) are now editable in-app alongside the other log settings. And a blank `usage.currency_symbol` falls back to the default symbol instead of rendering an empty badge. ([#564](https://github.com/schubydoo/clauster/pull/564))
- Security hardening (batch). The reverse-proxy `trusted_ips` allowlist is now validated as IP/CIDR at load — a malformed entry fails fast instead of silently never matching at runtime. The opt-in webhook SSRF guard (`webhooks.block_private_targets`) now **resolves DNS hostnames** and drops a URL whose name points at an internal IP, closing the hostname-bypass (a rebinding domain that re-resolves between the check and the POST remains an acknowledged TOCTOU residual). The clone `allow_private_hosts` description now states the field semantics rather than the default's effect. Internal hardening: direct unit tests for the hosted-stream redactor, a parity test pinning that every WebSocket endpoint enforces the same auth gate, and an inline note documenting why the session cookie's `SameSite=Lax` is safe (state changes are independently Origin-gated). ([#565](https://github.com/schubydoo/clauster/pull/565))
- Config editor: the **Default permission mode** dropdown now shows the same friendly labels as the "Run Claude here" launch menu (e.g. "Ask each time (default)", "Plan only (read-only)") instead of bare enum tokens — the saved value is unchanged. The **recap size limit** now greys out when "Recap prior transcript on restart" is off, and the **"Where new sessions run"** and **"Enable metrics line"** descriptions were rewritten to say what they actually do (bridge launches only; the per-session CPU / memory / disk-I/O line). ([#544](https://github.com/schubydoo/clauster/pull/544))
- Hosted sessions: fix `ProcessStream` subscriber leaks. `HostedSession.start()` and `reattach()` now drop their stream subscription if the spawn/reattach RPC fails or times out (the error path previously left an undrained subscriber on the stream), and the pump loop drops its subscription whenever it exits on its own — a natural agent exit or a daemon-loss error — rather than leaving it until a later `stop()`/`detach()`. ([#552](https://github.com/schubydoo/clauster/pull/552))
- Dashboard: the live-tail "reconnecting…" and "disconnected" banners no longer appear falsely while the log is streaming. A `d-flex` utility class (`display:flex !important`) was overriding the inline `display:none` that `x-show` uses to hide them, so both banners stayed pinned visible whenever the log panel was open — regardless of the tail's actual state. The flex layout now lives on an inner wrapper, leaving `x-show` free to hide the banner. ([#538](https://github.com/schubydoo/clauster/pull/538))
- Dashboard polish: the UI no longer shows raw enum tokens where a friendly label exists. The **Run** button and the hosted-session row now read "Auto-accept edits" / "Never prompt" etc. instead of `acceptEdits` / `dontAsk`; the Active-zone **filter chips** use the launch-menu product names (Desktop / Browser / Fire-and-forget) instead of internal tokens; and the config editor's **launch-mode, spawn-mode, and usage-mode** dropdowns get the same friendly labels as permission mode (the saved value is unchanged). Also fixes two accessibility gaps: the "First prompt" input and the New-project "Type" radio group are now properly associated with their labels for screen readers. ([#547](https://github.com/schubydoo/clauster/pull/547))
- Projects zone: the "Show all / Show fewer" toggle now appears in **every** sort, not only A–Z. In a last-used or cost sort the 6-row cap now applies by the chosen sort order — showing the top 6 with the toggle revealing the rest — instead of silently showing the full list with no toggle. A project with a live session is still never hidden by the cap. ([#545](https://github.com/schubydoo/clauster/pull/545))
- pty bridges: the keeper now waits on its PTY with `poll()` instead of `select()`. `select.select()` raises "filedescriptor out of range in select()" once the PTY master file descriptor reaches FD_SETSIZE (1024) — which a long-lived Clauster managing many bridges/keepers (or running on a busy host with many open fds) could hit, crashing the keeper's read loop. `poll()` has no such ceiling. ([#557](https://github.com/schubydoo/clauster/pull/557))
- Security (redaction): the `sk-…` token pattern in the log/stream redactor now includes `_` in its character class, matching the GitHub/GitLab/clauster token patterns. Anthropic `sk-ant-…` keys can contain underscores — without this, such a key would not fully redact and could leak into the bridge debug log or the live-tail WebSocket stream. ([#551](https://github.com/schubydoo/clauster/pull/551))
- Bridge runner: fix a race where `stop()` could orphan a bridge. `stop()` read `bridge_pid` without holding the per-project spawn lock, so a stop arriving while `spawn()` was suspended in `to_thread(_popen)` would see `bridge_pid=None`, mark the instance STOPPED, and return — leaving the freshly-spawned bridge running but untracked. `stop()` now takes the same lock `spawn()`/`forget()`/`resume()` use, so it waits for any in-flight spawn to publish its pid before reading it. ([#553](https://github.com/schubydoo/clauster/pull/553))

#### Dashboard: unified the two pre-launch warning vocabularies (UX-07). Both the header ([#560](https://github.com/schubydoo/clauster/pull/560))

system-wide pill and the per-project pill now read as "readiness checks" — the header
tooltip says "System readiness … affects every launch on this host", the per-project
pill drops the "preflight" jargon for "N check(s)" with a "for this project before
launch" tooltip and a "Readiness checks for &lt;project&gt;" detail heading. One term,
scoped by wording; internal names, API routes, and test hooks are unchanged.

#### Renamed the confusing `claude.resume_mode` config key to `claude.launch_mode` (#540). The ([#561](https://github.com/schubydoo/clauster/pull/561))

old name read like a resume on/off toggle, but the field actually picks the bridge launch
mode (`standard` vs `pty`). Existing `clauster.yml` files keep working: the legacy
`claude.resume_mode` key — and the `CLAUSTER_CLAUDE_RESUME_MODE` env var — are still
accepted as deprecated aliases that map to `launch_mode` with a warning (if both the old
and new key are set, the new one wins). Config-editor label and docs updated.

## 0.12.2 (2026-06-23)

[Compare with 0.12.1](https://github.com/schubydoo/clauster/compare/v0.12.1...v0.12.2)

### Features

- The Projects zone gains an optional sort control (Name / Last used / Cost). It defaults to the existing A–Z order and never reorders on its own — only when you pick a non-name sort does the list reorder (most-recent or highest-cost first, projects with no recorded history sinking to the bottom) and reveal every project. A new read-only `/api/projects/sortmeta` endpoint supplies the last-used and cost keys from the session-history rollup; the sort itself happens client-side and degrades silently to name order if the data can't be read. ([#528](https://github.com/schubydoo/clauster/pull/528))
- Record bridge/session lifecycle events (spawned, ready, ended, crashed) with mode and an end-of-session cost/token snapshot to a persistent session-history table, with per-project "last used / total cost" rollups readable from the DB. ([#514](https://github.com/schubydoo/clauster/pull/514))
- Expand the outbound webhook taxonomy beyond the four bridge events with `bg-settled` (a `claude --bg` background job settled), `permission-needed` (a hosted session parked a tool-permission prompt — the "come look" signal), and `clone-done` (a project clone finished). Each carries an `event_type` discriminator, is redacted before egress, and **defaults OFF** — opt in per-key under `webhooks.events`. ([#518](https://github.com/schubydoo/clauster/pull/518))
- Webhooks gain an opt-in SSRF guard. Set `webhooks.block_private_targets: true` to skip any webhook URL whose host is an internal/non-routable IP literal — loopback, link-local (incl. the `169.254.169.254` cloud-metadata IP), RFC1918 private, unspecified (`0.0.0.0`/`::`), reserved, multicast, IPv6 ULA (`fc00::/7`), and carrier-grade NAT (`100.64/10`). It also catches the non-canonical IPv4 encodings the OS resolver still dials but `ipaddress` rejects (decimal-integer `2130706433`, hex, short `127.1`), and normalizes IPv4-mapped IPv6, so none of those slip past to loopback or the metadata endpoint. Defaults to **off**, so existing LAN/private receivers keep working unchanged. DNS hostnames are not resolved (rebinding) and exotic IPv6 embeddings (NAT64, IPv4-compatible) are not normalized — out of scope for this literal-IP seam. ([#529](https://github.com/schubydoo/clauster/pull/529))
- New Clauster brand. A logo lockup (the carved-cells mark + `clauster` wordmark) now appears in the dashboard navbar and on the login and 404 pages, with a refreshed mark as the favicon. The lockup adapts to the theme — white on dark, black on light — and keeps the neon-green "live session" accent on both. The full logo kit (mark, wordmark, app icon, mono + accent variants, favicon) ships in `assets/logo/` alongside a brand showcase. ([#494](https://github.com/schubydoo/clauster/pull/494))
- Extend the bridge subprocess `PATH`/env from `clauster.yml` via `claude.path_append` and `claude.env` (both standard and pty modes), so a `claude` session can resolve user-local tools a minimal service `PATH` omits. ([#504](https://github.com/schubydoo/clauster/pull/504))

### Fixes

- Unify the hosted-session status badge colours with the bridge map in the shared Active list: `starting` is azure, `stopping` is orange, and `crashed` is amber (recoverable, Resume) instead of red — and the crashed badge now matches its dot. ([#505](https://github.com/schubydoo/clauster/pull/505))
- Serialize `SessionRunner` state persistence so a concurrent prune-races-upsert window can no longer surface a transient "could not persist bridge state" warning: the persist path now holds its own lock (mirroring the hosted manager), keeping each save atomic against interleaving startup-watch / stop / poll-loop writers. ([#501](https://github.com/schubydoo/clauster/pull/501))
- Harden pty-keeper sidecar parsing so a malformed `keeper_pid`/`bridge_pid` of `true`/`false` no longer resolves to PID 1 (`bool` is an `int` subclass) and is now treated as absent. ([#500](https://github.com/schubydoo/clauster/pull/500))
- Document the three `logs.retention_*` knobs in `clauster.yml.example`. ([#502](https://github.com/schubydoo/clauster/pull/502))
- Forget now clears a dead background agent even when `claude rm` soft-fails: clauster drops the orphaned job record itself, gated on the worker being confirmed dead so a live worker is never force-forgotten. ([#506](https://github.com/schubydoo/clauster/pull/506))
- Logs: the live-tail "reconnecting…" and "disconnected" banners are now mutually exclusive, and the disconnect banner no longer claims the bridge may have stopped while it is still running. ([#507](https://github.com/schubydoo/clauster/pull/507))
- Surface the config-file search order in `clauster --help`: the epilog now lists `$CLAUSTER_CONFIG` → `./clauster.yml` → `$CLAUSTER_HOME/clauster.yml`, so the resolution order is discoverable from the CLI without digging through the docs. ([#522](https://github.com/schubydoo/clauster/pull/522))
- A PTY-form bridge that survives a clauster restart is now reattached as a managed RUNNING instance instead of being orphaned as a STOPPED card or leaking an uncontrollable keeper process. ([#535](https://github.com/schubydoo/clauster/pull/535))

### Security

- Gate inline scripts with a per-request CSP nonce and drop `'unsafe-inline'` from `script-src` (`'unsafe-eval'` and `style-src 'unsafe-inline'` remain, tracked as the #442 follow-up). ([#532](https://github.com/schubydoo/clauster/pull/532))
- Store the `/metrics` scrape token as a SHA-256 hash at rest (`observability.metrics_token_hash`), matching the API token; mint one with the new `clauster hash-metrics-token` command. ([#503](https://github.com/schubydoo/clauster/pull/503))

### Build System & Dependencies

- Add a `changeset-autodraft` workflow that auto-drafts a `.changeset/*.md` fragment on trusted, same-repo PRs that touch `src/` but lack one. ([#530](https://github.com/schubydoo/clauster/pull/530))

## 0.12.1 (2026-06-20)

[Compare with 0.12.0](https://github.com/schubydoo/clauster/compare/v0.12.0...v0.12.1)

### Fixes

- Harden two untrusted-input readers against malformed input (both surfaced by new fuzz harnesses): the CSRF/CORS origin check no longer returns a 500 on an `Origin` header with an out-of-range or non-numeric port, and project discovery no longer crashes on a non-dict `~/.claude.json` — each now degrades safely ([#492](https://github.com/schubydoo/clauster/pull/492)).
- Toast notifications now render fully opaque instead of ~50% transparent, so they stay readable over busy page content ([#484](https://github.com/schubydoo/clauster/pull/484)).

## 0.12.0 (2026-06-19)

[Compare with 0.11.0](https://github.com/schubydoo/clauster/compare/v0.11.0...v0.12.0)

### Features

* **agents:** add a Resume action for ended background agents ([#336](https://github.com/schubydoo/clauster/issues/336)) ([#398](https://github.com/schubydoo/clauster/issues/398)) ([92e0735](https://github.com/schubydoo/clauster/commit/92e0735c6cb7d824ffafd9cfaabbd230ff36ba82))
* **auth:** API-token (Bearer) auth primitive ([#360](https://github.com/schubydoo/clauster/issues/360)) ([#380](https://github.com/schubydoo/clauster/issues/380)) ([16e0819](https://github.com/schubydoo/clauster/commit/16e081979b22ebad7063467571e93d28f4f2388a))
* **auth:** harden login lockout for the reverse-proxy case ([#358](https://github.com/schubydoo/clauster/issues/358)) ([#391](https://github.com/schubydoo/clauster/issues/391)) ([27ef771](https://github.com/schubydoo/clauster/commit/27ef771f9888764752fad872ebc78775357c8fa6))
* **cli:** stop an orphaned pty keeper not on any card — clauster keepers ([#301](https://github.com/schubydoo/clauster/issues/301)) ([#397](https://github.com/schubydoo/clauster/issues/397)) ([a81181a](https://github.com/schubydoo/clauster/commit/a81181ada05d6dbb2982f28b540b15f26339dcee))
* **config:** support CLAUSTER_*_FILE secret indirection ([#368](https://github.com/schubydoo/clauster/issues/368)) ([#385](https://github.com/schubydoo/clauster/issues/385)) ([613831d](https://github.com/schubydoo/clauster/commit/613831d6623f360215cc8aed75654a56373d6d81))
* **config:** wire the session-name-prefix + capacity bridge knobs ([#294](https://github.com/schubydoo/clauster/issues/294)) ([521d523](https://github.com/schubydoo/clauster/commit/521d523485e9140cf0a329d8c699b6a723f42049))
* **db:** persistence foundation — SQLAlchemy 2.0 + Alembic ([#362](https://github.com/schubydoo/clauster/issues/362)) ([#382](https://github.com/schubydoo/clauster/issues/382)) ([ba873f2](https://github.com/schubydoo/clauster/commit/ba873f2da74f0b3fc009d6c6cefe0e2ec5b3b998))
* **fe1b:** per-project preflight readiness pill ([#335](https://github.com/schubydoo/clauster/issues/335)) ([0ddb104](https://github.com/schubydoo/clauster/commit/0ddb104ede3b96641d6c51e6e5c83ef20df20e05))
* **fe3:** in-app config editor — safe-allowlist writes ([#299](https://github.com/schubydoo/clauster/issues/299)) ([#331](https://github.com/schubydoo/clauster/issues/331)) ([3fa5972](https://github.com/schubydoo/clauster/commit/3fa5972b04290509cd4284a877ff317cc2201947))
* **fe4b:** adopt a standard external session into a managed bridge ([#330](https://github.com/schubydoo/clauster/issues/330)) ([#341](https://github.com/schubydoo/clauster/issues/341)) ([797c796](https://github.com/schubydoo/clauster/commit/797c796be2e37fef02bcbc74c3416b5a4f44e7d5))
* **fe4:** rich external-session display ([#300](https://github.com/schubydoo/clauster/issues/300), display half) ([#329](https://github.com/schubydoo/clauster/issues/329)) ([c91ffda](https://github.com/schubydoo/clauster/commit/c91ffdab16ce85549272b074ee3d5309bf0651ee))
* **fe:** gate the hosted launch mode on CLAUSTRUM_ENABLED ([#375](https://github.com/schubydoo/clauster/issues/375)) ([ea08db5](https://github.com/schubydoo/clauster/commit/ea08db55a5b59b64dcd3282f19d408308eac0c4d)), closes [#345](https://github.com/schubydoo/clauster/issues/345)
* **install:** bump to 0.11.0 + complete Homebrew/Nix arches (macOS-x86_64, Linux-arm64) ([#289](https://github.com/schubydoo/clauster/issues/289)) ([2c5a610](https://github.com/schubydoo/clauster/commit/2c5a610f4eb82761764a389425a0e72c6cb64ddb))
* **logging:** implement JSON logging gated on log_format ([#361](https://github.com/schubydoo/clauster/issues/361)) ([#393](https://github.com/schubydoo/clauster/issues/393)) ([fa6c962](https://github.com/schubydoo/clauster/commit/fa6c9620788f71fcf216b6c360bb33aab90dab2d))
* **logs:** add retention/rotation for bridge debug logs ([#348](https://github.com/schubydoo/clauster/issues/348)) ([#387](https://github.com/schubydoo/clauster/issues/387)) ([664113e](https://github.com/schubydoo/clauster/commit/664113e8314949aed92cfa8eb8b6a3602eb98b84))
* **metrics:** enrich /metrics + add a scrape token ([#352](https://github.com/schubydoo/clauster/issues/352)) ([#389](https://github.com/schubydoo/clauster/issues/389)) ([0d32ec7](https://github.com/schubydoo/clauster/commit/0d32ec771e967540a99edf827111149bb3346f18))
* **security:** add HTTP security-headers middleware ([#439](https://github.com/schubydoo/clauster/issues/439)) ([7e89975](https://github.com/schubydoo/clauster/commit/7e89975bc576b3d2616f573b555d9fdf5d7c7a3e))
* **ui:** surface cloud-visible (bridge) vs local-only (hosted) channel ([#343](https://github.com/schubydoo/clauster/issues/343)) ([#396](https://github.com/schubydoo/clauster/issues/396)) ([33e4180](https://github.com/schubydoo/clauster/commit/33e418054812c573851ce722426f795fb84bf218))
* **webhooks:** outbound lifecycle webhooks — the first extension seam ([#371](https://github.com/schubydoo/clauster/issues/371)) ([#399](https://github.com/schubydoo/clauster/issues/399)) ([f773bc2](https://github.com/schubydoo/clauster/commit/f773bc2e33a34f7a5c76ebd8d0349119a56265b2))

### Bug Fixes

* **a11y:** announce live updates, honor reduced-motion, fix the Active-filter dead-end ([#384](https://github.com/schubydoo/clauster/issues/384)) ([4ecb50e](https://github.com/schubydoo/clauster/commit/4ecb50ee0b89082417411198f6b393167977e461))
* **a11y:** toast label/role, hosted status dot, resume single-flight ([#447](https://github.com/schubydoo/clauster/issues/447)) ([a153e08](https://github.com/schubydoo/clauster/commit/a153e08106cfbbeece3938f7f4eac3f42bb60fb5))
* **auth:** evict empty LoginThrottle per-key entries (unbounded growth) ([#436](https://github.com/schubydoo/clauster/issues/436)) ([756ddac](https://github.com/schubydoo/clauster/commit/756ddac9e3db76f07a247d8294edc95e2aff48fe)), closes [#402](https://github.com/schubydoo/clauster/issues/402)
* **cli:** gate keeper classification + kill on the keeper cmdline, not the PID alone ([#470](https://github.com/schubydoo/clauster/issues/470)) ([730f472](https://github.com/schubydoo/clauster/commit/730f472fc91a133d0d73d9fec478bb0f358d929a))
* **deps:** pin atheris per-interpreter so the 3.11 CI leg keeps a wheel ([#464](https://github.com/schubydoo/clauster/issues/464)) ([16fb12e](https://github.com/schubydoo/clauster/commit/16fb12e83990fdbc58484718d3cf4e1c0cf09a92))
* **hosted:** size the subscriber queue to hold a full replay snapshot ([#437](https://github.com/schubydoo/clauster/issues/437)) ([6d39af8](https://github.com/schubydoo/clauster/commit/6d39af8bc5fb8c1f32ff8258ffc2c232c1287cbc))
* **repo-config:** correct settings baseline (homepage + has_projects) ([#325](https://github.com/schubydoo/clauster/issues/325)) ([b5e74e4](https://github.com/schubydoo/clauster/commit/b5e74e497764aa107daff5cfedce92406f086936))
* **repo-config:** verify admin-only merge flags via a token-split drift check ([#328](https://github.com/schubydoo/clauster/issues/328)) ([e3b43c1](https://github.com/schubydoo/clauster/commit/e3b43c13be69adf1a997052f1ce18ea5967ed403))
* **runner:** guard _persist() against the OSError its store contract promises ([#435](https://github.com/schubydoo/clauster/issues/435)) ([c902de9](https://github.com/schubydoo/clauster/commit/c902de9602f74ce22d561bbe1df1eccfbca38cee)), closes [#420](https://github.com/schubydoo/clauster/issues/420)
* **security:** config backup inherits the source file mode, not the umask default ([#469](https://github.com/schubydoo/clauster/issues/469)) ([2a93f9c](https://github.com/schubydoo/clauster/commit/2a93f9c4a8291800cb52c9516992a14627bc3b52))
* **security:** low-severity hardening batch ([#452](https://github.com/schubydoo/clauster/issues/452)) ([088d1a9](https://github.com/schubydoo/clauster/commit/088d1a9354cc2c952dec628c73dfac5a728e0cfc))
* **security:** Referrer-Policy same-origin — native login/logout forms keep their Origin ([#465](https://github.com/schubydoo/clauster/issues/465)) ([acf6ad8](https://github.com/schubydoo/clauster/commit/acf6ad8248ca2200ba0732f38b8941ed73e09429))
* **ui:** auto-reconnect the bridge-log live tail instead of stranding it ([#419](https://github.com/schubydoo/clauster/issues/419)) ([260873b](https://github.com/schubydoo/clauster/commit/260873b214b1a7f9bfeced3d5d6e3f88f10e35c4))
* **ui:** cap openHosted live-view reconnect attempts to match the bridge-log tail ([#444](https://github.com/schubydoo/clauster/issues/444)) ([0f85224](https://github.com/schubydoo/clauster/commit/0f8522455cc40675186a3ce6b1f71e00cf238334))
* **ui:** clarify permission modes, the connect-URL gap, and config-restart impact ([#457](https://github.com/schubydoo/clauster/issues/457)) ([a94c429](https://github.com/schubydoo/clauster/commit/a94c42918f99c0d888084ecb6d6a13e0efcbadfa))
* **ui:** decouple the Recent zone from the live-session filter ([#451](https://github.com/schubydoo/clauster/issues/451)) ([a61789c](https://github.com/schubydoo/clauster/commit/a61789cda07562a72a1cb0d8e59a25f5ec23f789))
* **ui:** label the detached-row action by liveness, not always "Stop" ([#332](https://github.com/schubydoo/clauster/issues/332)) ([7d09545](https://github.com/schubydoo/clauster/commit/7d09545b37ec95ef34152acd1a81b5477b704791))
* **ui:** only label the Recent group "resumable" when it actually is ([#334](https://github.com/schubydoo/clauster/issues/334)) ([a1168ee](https://github.com/schubydoo/clauster/commit/a1168ee3976a35efed7f3dd8c0d159f5aeead2ce))
* **ui:** reset busy-flag in finally across action handlers (401 wedge) ([33104ac](https://github.com/schubydoo/clauster/commit/33104aca00188f633948c78796a0467b6e792543)), closes [#401](https://github.com/schubydoo/clauster/issues/401)
* **ui:** stop the hosted live-view dropping frames + repaint through the proxy ([#315](https://github.com/schubydoo/clauster/issues/315)) ([57deda2](https://github.com/schubydoo/clauster/commit/57deda2dcf2a411eafd41b10f0db9f67d5e1715b))
* **ui:** surface a crashed bridge's error_detail on the card ([#321](https://github.com/schubydoo/clauster/issues/321)) ([a659bda](https://github.com/schubydoo/clauster/commit/a659bdae3349e5e75e708160f6827897e3b5d276))

### Performance

* cache project discovery + batch first-paint preflight ([#440](https://github.com/schubydoo/clauster/issues/440)) ([baa96e2](https://github.com/schubydoo/clauster/commit/baa96e24429e42b2bcc2828230489b8b31f25c70))
* **metrics:** serve per-bridge metrics from a server-side cache ([#354](https://github.com/schubydoo/clauster/issues/354)) ([#390](https://github.com/schubydoo/clauster/issues/390)) ([00a2f5b](https://github.com/schubydoo/clauster/commit/00a2f5bb0e219e029a5a4b635e06394c2c920164))
* **runner:** sample bridge metrics concurrently ([ef2dd9d](https://github.com/schubydoo/clauster/commit/ef2dd9d19bb250822d765bc3e1282c90818cdaee)), closes [#407](https://github.com/schubydoo/clauster/issues/407)
* **ui:** batch live-metrics poll via /api/metrics ([#438](https://github.com/schubydoo/clauster/issues/438)) ([b2ec58c](https://github.com/schubydoo/clauster/commit/b2ec58cd9753b89bcdcd1a00cc0a65bfb2dcdb6f))
* **usage:** cache transcript rollup keyed on transcript-dir stamp ([#455](https://github.com/schubydoo/clauster/issues/455)) ([2c7f30d](https://github.com/schubydoo/clauster/commit/2c7f30d07ebdde6a374aec91a6d636d71aaf41d0))
* **web:** gzip responses + immutable cache for versioned static assets ([#353](https://github.com/schubydoo/clauster/issues/353)) ([#386](https://github.com/schubydoo/clauster/issues/386)) ([a514e1e](https://github.com/schubydoo/clauster/commit/a514e1eff1285b43a280bf7e86c57fa5ad9be76c))

## 0.11.0 (2026-06-15)

[Compare with 0.10.0](https://github.com/schubydoo/clauster/compare/v0.10.0...v0.11.0)

### Features

* **install:** one-line installers, Scoop/Homebrew/Nix + release auto-bump ([#287](https://github.com/schubydoo/clauster/issues/287)) ([feb666f](https://github.com/schubydoo/clauster/commit/feb666f645f50e962f4ce7964282f6848da8a54a))

## 0.10.0 (2026-06-15)

[Compare with 0.9.0](https://github.com/schubydoo/clauster/compare/v0.9.0...v0.10.0)

### Features

* **ops:** make install-service --write install the unit + actionable systemd doctor fix ([#267](https://github.com/schubydoo/clauster/issues/267)) ([bf32d00](https://github.com/schubydoo/clauster/commit/bf32d0071b35811a02d3e459d178bd84a01f0397))
* **sessions:** Forget a stopped session to clear it from Recent/resumable ([#268](https://github.com/schubydoo/clauster/issues/268)) ([7b68db8](https://github.com/schubydoo/clauster/commit/7b68db8cb9b7214b1669ff7fe0cedf2d1202eeb1))

### Bug Fixes

* **recap:** make the SessionStart hook survive a frozen one-file binary ([#279](https://github.com/schubydoo/clauster/issues/279)) ([6caa0ae](https://github.com/schubydoo/clauster/commit/6caa0ae8cdc3edb32904ba2623187b3fc9511302))
* **ui:** clarify launch Mode label + gate Spawn selector to the standard bridge ([#265](https://github.com/schubydoo/clauster/issues/265)) ([fefc3d9](https://github.com/schubydoo/clauster/commit/fefc3d9b1a28c296be9004a5293cfb7cd55556fc))
* **ui:** make detached & hosted Stop/Kill/Resume clickable; honest claude.ai framing ([#266](https://github.com/schubydoo/clauster/issues/266)) ([fdfd326](https://github.com/schubydoo/clauster/commit/fdfd326af97fb52317d030ed27bac60e4f3bf7fa))

### Performance

* **test:** shrink hosted stop-grace so the suite isn't 30s slower ([#275](https://github.com/schubydoo/clauster/issues/275)) ([3a1cd76](https://github.com/schubydoo/clauster/commit/3a1cd764caed15c705c82d1b8343483cffbcd342))

## 0.9.0 (2026-06-14)

[Compare with 0.8.0](https://github.com/schubydoo/clauster/compare/v0.8.0...v0.9.0)

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

## 0.8.0 (2026-06-07)

[Compare with 0.7.0](https://github.com/schubydoo/clauster/compare/v0.7.0...v0.8.0)

### Features

* add gated Prometheus /metrics endpoint ([#178](https://github.com/schubydoo/clauster/issues/178)) ([adac1c6](https://github.com/schubydoo/clauster/commit/adac1c65ea54ec5e486624b57e8a2102846a8fab))
* add read-only /api/widget summary endpoint ([#179](https://github.com/schubydoo/clauster/issues/179)) ([b38f71d](https://github.com/schubydoo/clauster/commit/b38f71dbc409f3092f0a7b439cb4317164ecd24f))
* **ui:** cards ⇄ rows dashboard layout toggle ([#173](https://github.com/schubydoo/clauster/issues/173)) ([da027f0](https://github.com/schubydoo/clauster/commit/da027f07e0338f94b6450eb22f689ba188ace231))
* **ui:** honest currency label on the cost badge (symbol only for USD) ([#167](https://github.com/schubydoo/clauster/issues/167)) ([ce5321c](https://github.com/schubydoo/clauster/commit/ce5321cdb6f54ab28494cc579a3f497094529812))
* **ui:** live per-bridge resource metrics (CPU / memory / disk) ([#172](https://github.com/schubydoo/clauster/issues/172)) ([bc2992e](https://github.com/schubydoo/clauster/commit/bc2992e5eaf4bf505f3591e079f1aa770a207bda))

### Bug Fixes

* **ci:** stop [@claude](https://github.com/claude) review from cancelling itself ([#183](https://github.com/schubydoo/clauster/issues/183)) ([abbd8bf](https://github.com/schubydoo/clauster/commit/abbd8bf6fb6f2a1dc6410290e04bd25739ee3d2f))
* **ui:** correct and clarify the permission-mode tooltip ([#165](https://github.com/schubydoo/clauster/issues/165)) ([5e6539e](https://github.com/schubydoo/clauster/commit/5e6539eeec8acf2b4329a24a6ac2171bc62d4b7b))

## 0.7.0 (2026-06-06)

[Compare with 0.6.0](https://github.com/schubydoo/clauster/compare/v0.6.0...v0.7.0)

### Features

* **ui:** actionable empty-state CTA ([#159](https://github.com/schubydoo/clauster/issues/159)) ([f382c1a](https://github.com/schubydoo/clauster/commit/f382c1aec4b5fddc03f7ba4169932b50efb50e50))
* **ui:** tooltips pass across the dashboard card ([#158](https://github.com/schubydoo/clauster/issues/158)) ([68c4009](https://github.com/schubydoo/clauster/commit/68c4009fbcd59c7233d6cff646b208ce8e804da0))

### Bug Fixes

* address four low-severity review findings ([#155](https://github.com/schubydoo/clauster/issues/155)) ([824c234](https://github.com/schubydoo/clauster/commit/824c23416a5ef26656bd7ed90fad9f764c27dce4))
* stop misclassifying a live clauster-launched pty bridge as external ([#153](https://github.com/schubydoo/clauster/issues/153)) ([3d12a6b](https://github.com/schubydoo/clauster/commit/3d12a6bfe0615e569518daeade90f78605c741cb))

## 0.6.0 (2026-06-05)

[Compare with 0.5.0](https://github.com/schubydoo/clauster/compare/v0.5.0...v0.6.0)

### Features

* **ui:** redesign the project card — clearer hierarchy, one primary action ([#143](https://github.com/schubydoo/clauster/issues/143)) ([8723498](https://github.com/schubydoo/clauster/commit/87234980fa2cbdb4c159675ee8aa6c831fa3d8a2))
* **ui:** trust-on-start — prompt to trust a directory at launch ([#144](https://github.com/schubydoo/clauster/issues/144)) ([110da36](https://github.com/schubydoo/clauster/commit/110da3600d4e1678b133b5de0c7b8c949df28505))

### Bug Fixes

* **doctor:** suppress the false "port in use" warning in the dashboard ([#142](https://github.com/schubydoo/clauster/issues/142)) ([5a56c5f](https://github.com/schubydoo/clauster/commit/5a56c5f826e833ffac4fb9d5a50182a196d01eaf))

## 0.5.0 (2026-06-05)

[Compare with 0.4.0](https://github.com/schubydoo/clauster/compare/v0.4.0...v0.5.0)

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

## 0.4.0 (2026-06-04)

[Compare with 0.3.0](https://github.com/schubydoo/clauster/compare/v0.3.0...v0.4.0)

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

## 0.3.0 (2026-06-03)

[Compare with 0.2.2](https://github.com/schubydoo/clauster/compare/v0.2.2...v0.3.0)

### Features

* **docker:** add a Docker Compose quickstart ([#97](https://github.com/schubydoo/clauster/issues/97)) ([e6c914d](https://github.com/schubydoo/clauster/commit/e6c914d22e648ede2dd3c0285a717fdae5f1bef2))
* **doctor:** check that the claude CLI is logged in ([#84](https://github.com/schubydoo/clauster/issues/84)) ([f902f23](https://github.com/schubydoo/clauster/commit/f902f23af6876f1cc95e450c0d4f1447c5e71cfe))

### Bug Fixes

* **runner:** serialize concurrent spawns of the same project ([#91](https://github.com/schubydoo/clauster/issues/91)) ([2dc8eb0](https://github.com/schubydoo/clauster/commit/2dc8eb044aaed3e735627f2feedbce8281a6b298))
* **runner:** stop wiping persisted metadata for untracked projects ([#92](https://github.com/schubydoo/clauster/issues/92)) ([cca1c69](https://github.com/schubydoo/clauster/commit/cca1c69ec29a48c2d034d64cdf5a23e4dca1383a))
* show Restart for stopped pty bridges so true-resume is reachable ([#99](https://github.com/schubydoo/clauster/issues/99)) ([5ea38aa](https://github.com/schubydoo/clauster/commit/5ea38aaa0b2ed92039681be4babe32c7ba9ad465))

## 0.2.2 (2026-06-03)

[Compare with 0.2.1](https://github.com/schubydoo/clauster/compare/v0.2.1...v0.2.2)

### Security

* **This is a security release.** A non-loopback bind (e.g. `0.0.0.0` or a LAN IP) could serve the dashboard **unauthenticated** when `auth.enabled` was left at its default `false` — even with a password configured — because the runtime guard only enforces auth when `auth.enabled` is set, while config validation did not require it. The config validator now refuses to start a non-loopback bind unless authentication is actually enforced (`auth.enabled: true` together with `auth.password_required` + a hash, or `auth.reverse_proxy.enabled`; or the explicit `auth.allow_unauthenticated_network` opt-out). All prior releases (≤ 0.2.1) are affected, including the Docker image. **Upgrade, and on any networked deployment set `auth.enabled: true`.** See [GHSA-h4g2-xfmw-q2c9](https://github.com/schubydoo/clauster/security/advisories/GHSA-h4g2-xfmw-q2c9).

### Bug Fixes

* **auth:** refuse non-loopback bind unless auth is actually enforced ([#88](https://github.com/schubydoo/clauster/issues/88)) ([d89d753](https://github.com/schubydoo/clauster/commit/d89d753120c2246eea1838cea9528aa7658eb36f))

## 0.2.1 (2026-06-03)

[Compare with 0.2.0](https://github.com/schubydoo/clauster/compare/v0.2.0...v0.2.1)

### Documentation

* absolute GitHub URLs in README so images render on PyPI ([#79](https://github.com/schubydoo/clauster/issues/79)) ([1feef42](https://github.com/schubydoo/clauster/commit/1feef42b91a82b2d31063aa448c90e5a0688fb6a))

## 0.2.0 (2026-06-03)

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
