# Changelog

## 1.1.0 (2026-08-30)

[Compare with 1.0.2](https://github.com/schubydoo/clauster/compare/v1.0.2...v1.1.0)

### Features

- Show the `claude` version each running bridge is actually on as a small muted label on its live session card, read from the live process tree and left blank when it can't be resolved. ([#1292](https://github.com/schubydoo/clauster/pull/1292))
- Restore a Direct Session's prior conversation in its View pane after a Clauster restart, rebuilt read-only from `claude`'s own transcript. ([#1300](https://github.com/schubydoo/clauster/pull/1300))
- Add a `log_level` config key (`debug` | `info` | `warning` | `error`, also settable as `CLAUSTER_LOG_LEVEL`) that raises Clauster's and uvicorn's server log verbosity, which was previously pinned at `info`. ([#1289](https://github.com/schubydoo/clauster/pull/1289))
- Add a **No forced mode** (`inherit`) launch choice that passes no `--permission-mode` flag at all, so a session starts in its own default mode instead of one forced into its spawn-time system prompt. ([#1290](https://github.com/schubydoo/clauster/pull/1290))

### Fixes

- Promote an adopted bridge out of "Starting" once its connect evidence appears, so a session taken over from another process no longer shows "preparing connect link…" forever. ([#1297](https://github.com/schubydoo/clauster/pull/1297))
- Probe the claustrum daemon with `server.capabilities` instead of the `server.version` method that claustrum v1.10 removes, so the connection keeps working across current and future daemon releases. ([#1281](https://github.com/schubydoo/clauster/pull/1281))
- Config and trust JSON writes now take their advisory lock on a file under `<state_dir>/locks/` instead of a `<file>.lock` sidecar, so editing a project's settings no longer drops a lock artifact inside its git-tracked tree. ([#1171](https://github.com/schubydoo/clauster/issues/1171)) ([#1286](https://github.com/schubydoo/clauster/pull/1286))
- `clauster doctor` now reads the module version Go embeds in the claustrum binary when `--version` reports only the unstamped `claustrum-dev` sentinel, so the row names the installed release instead of shrugging "unstamped/dev or older build". ([#1283](https://github.com/schubydoo/clauster/pull/1283))
- Give every dashboard form control a stable `id`/`name` and an accessible name, clearing the browser's form-field accessibility advisories for screen-reader and password-manager users. ([#1294](https://github.com/schubydoo/clauster/pull/1294))
- Report the range a Direct Session's daemon replay buffer evicted on reattach — an "output lost" marker leads the replayed stream instead of the gap being skipped silently. ([#1299](https://github.com/schubydoo/clauster/pull/1299))
- Persist each Interactive Session keeper's start time so `forget` can tell that keeper from a different one that later reused its process id, instead of refusing to clear the record. ([#1298](https://github.com/schubydoo/clauster/pull/1298))
- Stop a rediscovered pty keeper from being carded under an unrelated session's instance id, which overwrote that session's persisted record. ([#1295](https://github.com/schubydoo/clauster/pull/1295))
- `clauster logs` now reads a stopped or crashed bridge's log from disk instead of refusing it, and says whether an id was unknown or simply has no log. ([#1284](https://github.com/schubydoo/clauster/pull/1284))
- Show each session row its own bridge's CPU/memory instead of the project total, so a Server Mode row no longer reports the co-located Interactive Sessions' usage as its own (Interactive Session rows now show metrics too). ([#1287](https://github.com/schubydoo/clauster/pull/1287))
- The resume picker no longer lists headless agent transcripts (`entrypoint: "sdk-py"`), which passed the sidechain filter. ([#1311](https://github.com/schubydoo/clauster/pull/1311))
- Leave dispatched-subagent (sidechain) transcripts out of the launch popover's Conversation picker so real conversations are no longer buried; they stay visible in the read-only Transcripts viewer. ([#1288](https://github.com/schubydoo/clauster/pull/1288))
- The transcript picker labels a slash-command-started session with its first human prompt instead of the `<local-command-caveat>` wrapper. ([#1318](https://github.com/schubydoo/clauster/pull/1318))
- Keep an Interactive Session's git worktree across a keeper-only reattach, so a rediscovered session resumes into its original worktree instead of creating a second one. ([#1296](https://github.com/schubydoo/clauster/pull/1296))
- A `CLAUSTER_PYTE_PATH`-provided pyte is now visible from startup, so the Live terminal no longer stays disabled until something else imports it. ([#1312](https://github.com/schubydoo/clauster/pull/1312))
- Bound the dashboard's optimistic "stopping" bridge row so a session the server has confirmed gone can no longer strand on screen forever. ([#1291](https://github.com/schubydoo/clauster/pull/1291))

## 1.0.2 (2026-08-28)

[Compare with 1.0.1](https://github.com/schubydoo/clauster/compare/v1.0.1...v1.0.2)

### Fixes

- Fix dashboard elements that could stay visible after a rapid hide→show→hide (an Alpine.js `x-show` cascade race under load) — the root of the e2e "Manage button never retires" flake. ([#1270](https://github.com/schubydoo/clauster/pull/1270))

## 1.0.1 (2026-08-21)

[Compare with 1.0.0](https://github.com/schubydoo/clauster/compare/v1.0.0...v1.0.1)

### Fixes

- Fix four bridge-lifecycle defects: keeper sidecars now match per project rather than by name prefix, a non-positive sidecar pid fails closed instead of raising out of startup, `forget` refuses a persisted-only record whose bridge or keeper is still alive, and `max_bridges` counts every live bridge including the spawning project's own. ([#1177](https://github.com/schubydoo/clauster/pull/1177))
- Fix six defects across the CLI and write surfaces: a malformed `clauster.yml` exits 2 with one line instead of a traceback (and `doctor` diagnoses it), the Windows service argv accepts a quoted config path, subagent write and delete run their guards inside the file lock, a non-object credentials file raises `CredentialsError` rather than failing a spawn, `claustrum` daemon status carries a reason for every failure, and one unreadable skill no longer breaks the skills listing. ([#1183](https://github.com/schubydoo/clauster/pull/1183))
- Fix the config editor accepting a bounded value it then rejects on save with a `422`. It now enforces the bound up front, including an exclusive endpoint that must be exceeded, not merely met. ([#1185](https://github.com/schubydoo/clauster/pull/1185))
- Reject a boolean `bridge_pid` in a pty keeper sidecar so a `"bridge_pid": true` value no longer persists PID 1 (alive on every host, making the row read live forever). (#1182) ([#1233](https://github.com/schubydoo/clauster/pull/1233))
- Serve rendered HTML pages with `Cache-Control: no-store` so a cached or bfcached copy can't replay a stale CSP nonce or survive a deploy as an outdated render. ([#1243](https://github.com/schubydoo/clauster/pull/1243))
- `clauster doctor` and the dashboard preflight panel no longer report `extra:pyte` as unavailable when pyte is side-loaded via `CLAUSTER_PYTE_PATH` — the env-var path is now placed on `sys.path` before the probe, matching the managed-deps behaviour. (#1193) ([#1234](https://github.com/schubydoo/clauster/pull/1234))
- A bridge left in `error` status (e.g. a Resume/spawn that failed to launch) no longer renders in neither dashboard zone and vanishes — it now stays in the Recent zone as a "Failed to start" card with its `error_detail` on screen, so a failed launch fails visibly instead of silently. (#1149) ([#1237](https://github.com/schubydoo/clauster/pull/1237))
- `clauster keepers` no longer hides (and `--kill` no longer refuses) a removed project's live keeper when a carded sibling shares its name prefix — e.g. a carded `app` used to swallow `app-staging`'s orphaned keeper. Protection now keys on the exact parsed `<name>` stem instead of an unanchored `<project>-*` glob. (#1181) ([#1236](https://github.com/schubydoo/clauster/pull/1236))
- In the Manage Claude Code config MCP panel, the "Save approvals" / "Reset choices" scope-token confirms are now hidden while a server delete is armed, so two identical "Type `<scope>` to confirm" inputs can no longer stack with no signal which action would fire. (#1043) ([#1235](https://github.com/schubydoo/clauster/pull/1235))
- `claude.node_from_nvm` now **prepends** nvm's resolved `default` node bin dir to the bridge `PATH` instead of appending it, so nvm's node wins over a distro `node` already on the inherited service PATH (e.g. `/usr/bin/node`). Appending let an earlier base node shadow nvm's, reintroducing the exact `npx`/`node` resolution failure the knob exists to fix — with the wrong node. Operator `claude.path_append` entries still append after the base. (#1018) ([#1238](https://github.com/schubydoo/clauster/pull/1238))
- Fix two phone-width layout breaks on the dashboard: the active-session action row (Live terminal / Transcript / Stop) now wraps instead of forcing the whole page to scroll sideways — so Stop is reachable on a phone — and the "Run Claude here" launch popover is pinned to the viewport below the `sm` breakpoint instead of hanging off the left edge. (#1159) ([#1240](https://github.com/schubydoo/clauster/pull/1240))
- Fix a bare project name silently resolving to the wrong bridge when a project has more than one instance (#1150) — after one start → stop → start cycle a project keeps two rows. Every by-name surface (`clauster stop`/`logs`/`open`, `GET /api/instances/{name}`, the QR and by-name WebSocket routes, and the `stop_session`/`resume_session` MCP tools) now refuses with an "ambiguous" error listing the candidate instance ids, matching the id-prefix behaviour (#1099). ([#1217](https://github.com/schubydoo/clauster/pull/1217))
- Stopping an Interactive Session (pty, `spawn_mode: worktree`) now releases the git lock Claude Code placed on the session's worktree, so `git worktree remove` no longer refuses it with a dead-pid lock reason. The worktree and its branch are still left in place (they may hold uncommitted work, and a resume reuses them); only the now-pointless lock is dropped. (#1089) ([#1239](https://github.com/schubydoo/clauster/pull/1239))
- Fix the MCP `resume_session` tool reporting a cap-declined resume as success (#1148): the one-live-standard-bridge-per-project cap hands back the already-live bridge, so a declined resume answered `resumed: true` with a bridge that was never revived. The tool now reads the full outcome (new `ClausterEngine.resume_detailed`) and answers `resumed: false` with a `reason`, mirroring `POST /api/instances/{id}/resume` (#1145). ([#1215](https://github.com/schubydoo/clauster/pull/1215))
- Fix three boundary defects: a non-ASCII setup token now returns 403 instead of 500, secrets nested inside a secret-shaped key are masked, and listing subagents no longer reads a plugin symlink's target. ([#1174](https://github.com/schubydoo/clauster/pull/1174))
- Fix workspace trust for Claude Code 2.1.232+: a nested git repository now requires its own trust grant (a parent grant no longer cascades), so the badge and spawn gate fail closed instead of showing green over a directory the CLI rejects, and a new dashboard "Trust all" action reconciles installs whose repos were trusted only via a parent. (#1224) ([#1232](https://github.com/schubydoo/clauster/pull/1232))

### Build System & Dependencies

- Float the Alpine apk package revision with a fuzzy version pin (`=~`) so an Alpine `-rN` rebuild no longer breaks the image build. ([#1220](https://github.com/schubydoo/clauster/pull/1220))

## 1.0.0 (2026-07-28)

[Compare with 0.12.9](https://github.com/schubydoo/clauster/compare/v0.12.9...v1.0.0)

<!-- highlights:start -->

### Highlights

Clauster 1.0 is the first stable release — a self-hosted dashboard for spawning and
managing Claude Code remote-control bridges on your own host. Highlights since 0.12.9:

- **Claude Code config management, in the browser.** A full dashboard editor and gated
  write API for a project's entire Claude Code config surface — CLAUDE.md, settings/env,
  permission rules, hooks, MCP servers, subagents, skills, and plugins/marketplaces —
  across User / Project / Local scopes, each behind a type-the-name confirm with secret
  masking. The release's largest new capability.
- **Multiple sessions per project, with reliable resume.** Run several interactive
  sessions per project (each in its own git worktree) alongside a standard bridge, fork
  any past conversation into a new session, give sessions custom names, and rely on a
  hardened self-heal path so a restarted bridge never comes back idle with no session.
- **The dashboard reports what is actually running.** Bridges are tracked per instance rather
  than per project, so every session gets its own card instead of one silently displacing the
  rest; Resume survives a restart instead of vanishing from stopped cards; and when the
  one-Server-Mode-bridge-per-project cap would decline a resume, the card says so before you
  click, rather than reporting a success that never happened.
- **Windows & cross-OS parity.** Interactive (true-resume PTY) sessions now run on Windows
  over ConPTY, the hosted channel speaks Windows named pipes, and state writes are hardened
  — bringing Windows to parity with POSIX across the 3-OS test matrix.
- **Drive it headless: CLI, MCP, and a versioned API.** Operate Clauster entirely from the
  terminal with no server running (`clauster projects/status/sessions/logs/open`,
  `start`/`stop`), inspect sessions from any MCP client, and integrate against a versioned
  `/api/v1` with named, revocable bearer tokens. The stdio MCP surface is **read-only by
  default** — it is local-privileged and unauthenticated, so the tools that spawn, stop and
  resume sessions are opt-in behind `mcp.allow_writes`.
- **Re-authenticate from the dashboard.** A browser login flow (`claude auth login` /
  `setup-token`) plus a header pill that surfaces a logged-out runtime account before a
  bridge silently hangs — no more SSHing in to fix auth.
- **Security & supply-chain hardening.** Dropped `unsafe-eval` from the CSP, auto-provisioned
  self-signed TLS, a correct reverse-proxy `root_path` guard, and an MCP-approval preflight
  that stops silent hangs.
- **Smaller, signed distribution.** A slimmer Alpine/musl Docker image with pinned packages,
  first-class `uninstall` scripts, and an optional-extras lifecycle where the standalone
  binary bundles pip and side-installs `pty`/`notify` on demand.
- **Committed to SQLite.** Storage is now SQLite-only (the unsupported Postgres `database_url`
  is gone), with an automatic `clauster.db` snapshot before every migration.

**Read Breaking Changes below before upgrading**, and [UPGRADING.md][upgrading] for the
step-by-step. The three most likely to need action from you:

- **Cross-site requests are now blocked even with no password set.** If you reach the
  dashboard at anything other than a loopback address at its configured port — a LAN bind,
  a tunnel or reverse proxy, an SSH forward onto a different port — that address must be in
  `auth.allowed_origins` or your buttons and live views stop working. It fails visibly, not
  silently. A default loopback deployment needs no change.
- **The Docker base moved to Alpine.** Derived images must use `apk` rather than `apt`, and
  `su-exec` replaces `gosu`.
- **`clauster mcp` changed twice.** Its write tools now default off (above), and a bridge's
  `id` is its unique instance id rather than the project name — if you consume
  `list_sessions`, read `project` for the name.

[upgrading]: https://github.com/schubydoo/clauster/blob/main/UPGRADING.md

<!-- highlights:end -->

### Breaking Changes

- The Docker image now uses an Alpine (musl) base with explicitly pinned, Renovate-tracked apk packages instead of Debian slim with an unpinned `apt upgrade`; it is smaller and drops the Go-based `gosu` (now `su-exec`). Derived images must use `apk` rather than `apt`. ([#944](https://github.com/schubydoo/clauster/pull/944))
- The `clauster mcp` write tools (`spawn_session` / `stop_session` / `resume_session`) are now gated behind a new `mcp.allow_writes` config key that defaults **off**, so the local-privileged, unauthenticated stdio MCP surface is read-only by default and its `--help`/banner no longer understate its capability (#1010); set `mcp.allow_writes: true` to restore the #950 write tools. ([#1066](https://github.com/schubydoo/clauster/pull/1066))
- The cross-site `Origin` allowlist is now enforced even when `auth.enabled` is false (the shipped default), closing a CSRF / WebSocket-hijack hole: previously the HTTP `guard` middleware returned before the Origin check and all four WebSocket routes short-circuited theirs, so a page the operator merely visited could drive unsafe requests against the loopback dashboard (trust a directory, resume a bridge, clone a repo, restart) and read-stream live session output. With auth off the gate rejects only a *present*, non-allowlisted `Origin` — an absent one (a CLI/script client, never a browser) still passes. A loopback bind auto-allows `127.0.0.1`/`localhost`/`[::1]` **at its configured port**, so the default deployment needs no change; an auth-off deployment the browser reaches at any other address — a non-loopback bind, a tunnel or reverse proxy, or an SSH port-forward onto a different local port — must now list that origin in `auth.allowed_origins`, the same setting an authenticated deployment in those positions already needs. It fails closed and visibly, never silently (see UPGRADING.md). ([#1070](https://github.com/schubydoo/clauster/pull/1070))
- Live sessions are now attributed to the bridge that actually owns them, so a Server Mode bridge no longer lists a project's independent Interactive Sessions as its own; the MCP `list_sessions` tool no longer repeats a session once per bridge, and a bridge's `id` is now its unique instance id (**breaking**: it was the project name — see UPGRADING.md). ([#1082](https://github.com/schubydoo/clauster/pull/1082))
- Commit to SQLite for storage and remove the unsupported `database_url` (Postgres) config key; a leftover `database_url` in your config is now ignored rather than rejected. ([#801](https://github.com/schubydoo/clauster/pull/801))

### Features

- The **Advanced** config panel now edits list and map fields too (#978): `clone.allowed_schemes` and `clone.allowed_private_cidrs` get a rows editor (add/remove entries), and `webhooks.events` gets a checkbox per known lifecycle event (each shown at its correct default, saving only the events you change). These join the existing Advanced scalars behind the same `config_write` capability + step-up re-auth. Secret URL lists (`webhooks.urls`, `notifications.urls`) and auth trust lists stay file/CLI-only — their masked values can't round-trip a browser edit safely — and every write is still fail-closed, re-validated (bad CIDR / unknown event rejected), and audited by key-name only. ([#982](https://github.com/schubydoo/clauster/pull/982))
- New **Advanced** config surface (#978): the operational-but-sensitive `clone.*` and `webhooks.*` scalars are now editable in-app behind the `config_write` capability **and** a step-up re-auth — `POST /api/reauth` re-proves the operator password for a short unlock window, and `GET`/`PUT /api/config/advanced` gate on that fresh proof. Lockout/exposure/RCE keys (bind, auth switches, `ui.enabled`, TLS, binaries, `config_write.*`, `login_shepherd.*`) stay file/CLI-only, and every Advanced write is fail-closed (backup + atomic replace) and audited by key-name only. ([#979](https://github.com/schubydoo/clauster/pull/979))
- The config editor now has an **Advanced settings** panel (#978): when config-write is enabled, the `clone.*` and `webhooks.*` scalars are editable from the dashboard behind a step-up re-auth — re-enter your password to unlock a short window, edit, and save through the same "restart to apply" flow. A wrong password shows an inline error and keeps the panel locked; the unlock window expiring mid-edit re-prompts rather than silently failing. Network bind, authentication, and TLS stay file/CLI-managed. ([#980](https://github.com/schubydoo/clauster/pull/980))
- Launching a bridge over the API now reports whether it was newly created or an existing one was reused, and each project's metrics add up across all of its bridges. ([#797](https://github.com/schubydoo/clauster/pull/797))
- Add two optional per-launch controls for standard (server-mode) bridges: a custom session name shown in claude.ai/code in place of the project name, and a sandbox toggle (OS-level filesystem/network isolation). Both default to no change and persist across resume. ([#811](https://github.com/schubydoo/clauster/pull/811))
- New headless read commands — `clauster projects`, `status`, `sessions`, `logs`, and `open` — drive clauster from the terminal with no server running (add `--json` for scriptable output), sharing one engine with the web UI. ([#927](https://github.com/schubydoo/clauster/pull/927))
- New headless write commands — `clauster start <project>` and `clauster stop <instance>` — spawn and stop bridges from the terminal with no server running, sharing the web UI's engine, mode policy, and validation (`--mode`, `--trust`, `--json`). ([#928](https://github.com/schubydoo/clauster/pull/928))
- Completes the config-change audit trail's subprocess-visibility slice (#958 Part 6): MCP **and plugin** writes now record the redacted `claude mcp`/`claude plugin` **argv** the CLI actually ran, and the before/after **file fingerprints** (path + SHA-256 + size, never contents) now cover the reset-project-choices, approvals, plugin, and marketplace handlers too — not just MCP-server writes. So `config_audit.log` answers both "where did this change land?" and "what ran?" for every CLI-driven config mutation. ([#977](https://github.com/schubydoo/clauster/pull/977))
- The config-change audit trail now records, for each MCP-server write, **which config files the change actually touched** — by path, SHA-256, and byte size (never the file contents). Because `claude mcp` does Claude Code's own bookkeeping across several files, this makes the subprocess's side effects visible in `config_audit.log` and answers "where did this change land?" without exposing any secret values. ([#976](https://github.com/schubydoo/clauster/pull/976))
- Every config-write is now recorded to a single `config_audit.log` audit trail — one JSON line per change across every surface (CLAUDE.md, settings, permissions, hooks, MCP, approvals, subagents, skills, plugins, marketplaces), capturing the surface, scope, target file, action, actor, and the top-level key names touched (never the values). Generalises the previous CLAUDE.md-only edit audit so a config change can be traced to where it landed. ([#969](https://github.com/schubydoo/clauster/pull/969))
- Add a Config management panel to the dashboard (when config-write is enabled) for editing your project's CLAUDE.md and settings.json across User/Project/Local scopes, with a type-the-name confirm and secret masking. ([#828](https://github.com/schubydoo/clauster/pull/828))
- Add an MCP tab to the Config management panel to add, edit, and remove MCP servers at project, local, or user scope, and to approve or reject each committed project server. ([#831](https://github.com/schubydoo/clauster/pull/831))
- Add Permissions and Hooks tabs to the Config management panel for editing a scope's tool-permission rules and lifecycle hooks, behind the same type-the-name confirm. ([#829](https://github.com/schubydoo/clauster/pull/829))
- Add a Plugins tab to the Config management panel to enable, disable, install, uninstall, and update plugins, and to add, remove, and update marketplaces — each gated by the type-the-name confirm, with an extra retype-the-id confirm on install. ([#833](https://github.com/schubydoo/clauster/pull/833))
- Add a Skills tab to the Config management panel to list, create, edit, and delete your project or user skills (their SKILL.md), behind the same type-the-name confirm. ([#832](https://github.com/schubydoo/clauster/pull/832))
- Add a Subagents tab to the Config management panel to list, create, edit, and delete your project or user subagents (built-in and plugin agents are shown read-only). ([#830](https://github.com/schubydoo/clauster/pull/830))
- Add a CLAUDE.md config-write surface (user/project/local scope, edit/blank, gitignore-on-create for the local file) — content is never redacted, unlike other config-write surfaces, since it's prose, not secrets. ([#818](https://github.com/schubydoo/clauster/pull/818))
- Add a `local` scope to the config-write API (MCP servers, permission rules, hooks) for project-only settings, gitignoring any newly created local config file automatically. ([#813](https://github.com/schubydoo/clauster/pull/813))
- MCP servers can now be added/removed/edited through Claude Code's own `claude mcp` CLI (secrets travel via env, never the command line), plus project approval enable/disable and a reset-approvals action — all behind the existing config-write gate. ([#822](https://github.com/schubydoo/clauster/pull/822))
- Add a plugins + marketplaces config-write API (enable/disable/install/uninstall/update, marketplace add/remove/list/update) driven through `claude plugin`, gated behind the existing off-by-default capability plus a strong per-plugin confirm on install. ([#827](https://github.com/schubydoo/clauster/pull/827))
- Add a config-write API for `env`/`model`/misc `settings.json` keys at user/project/local scope, plus a scope-merge provenance view showing each key's effective value and source layer. ([#823](https://github.com/schubydoo/clauster/pull/823))
- Add a skills config-write surface (user/project scope; list/view/add/delete + `skillOverrides` enable/disable at all three scopes) — plugin skills stay read-only, uploaded scripts need an extra confirm and are never executed, and skill content is redacted on read. ([#825](https://github.com/schubydoo/clauster/pull/825))
- Add a subagents config-write surface (list/view/add/edit/delete at user/project scope) — Claude Code's own built-in and plugin-provided agents are shown but refused as read-only. ([#824](https://github.com/schubydoo/clauster/pull/824))
- Harden the dashboard's Content-Security-Policy by removing `unsafe-eval` from script-src. ([#788](https://github.com/schubydoo/clauster/pull/788))
- Run several interactive sessions per project from the dashboard, each with its own controls and isolated git worktree, plus a warning when two would share the same working copy. ([#800](https://github.com/schubydoo/clauster/pull/800))
- `clauster deps install claustrum` now side-installs the **Direct Session daemon** — the standalone `claustrum` binary the hosted live-view channel needs. It downloads the pinned, SHA-256-verified release for your OS/arch (Linux/macOS/Windows × x86_64/arm64) from `schubydoo/claustrum` into `<state_dir>/deps/bin`, and Clauster's daemon launcher uses it automatically (an explicit `claustrum.binary` or a `claustrum` on `PATH` still wins). Fail-closed like the other managed binaries — a checksum mismatch refuses — and surfaced in `clauster deps list` / `doctor` (the doctor line only appears when `claustrum.enabled`). The pin auto-bumps via Renovate. ([#984](https://github.com/schubydoo/clauster/pull/984))
- New `clauster deps list/install/uninstall <extra>` side-installs the optional `pty`/`notify` extras into `<state_dir>/deps` for the standalone binary (which adds that directory to its import path at startup), behind an explicit unsigned-wheel confirmation. ([#933](https://github.com/schubydoo/clauster/pull/933))
- First-run setup wizard (#978): running `clauster` with no `clauster.yml` now serves a small, loopback-only setup page instead of exiting with an error. Enter your projects folder, bind address, and a dashboard password, and it writes a config with authentication enabled, then restarts onto it. The wizard binds `127.0.0.1:7621` by default (override with `CLAUSTER_SETUP_PORT`) and gates the submit to a loopback origin, so only a local operator can complete setup. ([#981](https://github.com/schubydoo/clauster/pull/981))
- The standalone binary now bundles `pip`, so `clauster deps install <extra>` runs on it directly (no separate Python needed); `clauster deps install shawl` provides the pinned, checksum-verified [Shawl](https://github.com/mtkennerly/shawl) service wrapper that `install-service windows` now uses instead of nssm; and the uninstaller enumerates the side-installed extras/Shawl and can preserve them with `--keep-deps`. ([#936](https://github.com/schubydoo/clauster/pull/936))
- The config panel's Hooks surface gains a friendly rows editor — add/remove one command hook per row (event, matcher, command, timeout) instead of hand-editing the nested JSON; rows sharing an event + matcher are saved as one group, and raw JSON stays as the escape hatch for shapes the rows can't show. Commands are stored verbatim and never run by Clauster. ([#973](https://github.com/schubydoo/clauster/pull/973))
- `clauster stop/logs/open`, the MCP tools and the API now accept a unique instance-id prefix, refusing an ambiguous one instead of guessing. ([#1113](https://github.com/schubydoo/clauster/pull/1113))
- Add an opt-in, auth-gated dashboard panel that drives a `claude auth login` / `claude setup-token` flow so an operator can re-authenticate the runtime `claude` account from the browser instead of SSHing in. ([#846](https://github.com/schubydoo/clauster/pull/846))
- The dashboard login panel now points operators at the non-interactive runtime-account auth options (`ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `apiKeyHelper`) so they can be set from the config editor without SSH, with docs on which to use when. ([#925](https://github.com/schubydoo/clauster/pull/925))
- The `clauster mcp` server gains write tools — an MCP client can now spawn, stop, and resume bridge sessions (not just observe them). Trust is never auto-granted: `spawn_session` requires an explicit `trust: true` for an untrusted directory. ([#950](https://github.com/schubydoo/clauster/pull/950))
- The `claude.node_from_nvm` setting now defaults to **on**: bridges resolve nvm's `default` node and put its `node`/`npx`/`npm` plus nvm-global CLIs (e.g. `agent-browser`) on the bridge `PATH` in every spawn context (not just `bash -c`). `clauster doctor` warns when an nvm toolchain won't reach spawned bridges; set the option false to opt out. ([#859](https://github.com/schubydoo/clauster/pull/859))
- Add an opt-in `claude.node_from_nvm` setting that puts your nvm-managed Node on the bridge's PATH, fixing npx/node MCP servers that failed to start under systemd. ([#803](https://github.com/schubydoo/clauster/pull/803))
- The config panel's Permissions surface gains a friendly rows editor — add/remove `allow`, `deny`, and `ask` rules and pick a default mode from a list, instead of hand-editing raw JSON (raw stays as the escape hatch for shapes the rows can't show). `ask` rules are now accepted by the permissions write surface. ([#972](https://github.com/schubydoo/clauster/pull/972))
- Automatically back up `clauster.db` before running a database migration, keeping the last 5 snapshots under `state_dir/backups/`. ([#804](https://github.com/schubydoo/clauster/pull/804))
- Interactive Session launches gain a Conversation picker (More options): fork any of the project's past conversations into the new session — the original stays untouched. ([#948](https://github.com/schubydoo/clauster/pull/948))
- Add a versioned `/api/v1` public API with named, revocable bearer tokens (`clauster api-token issue/list/rotate/revoke`) and an optional OpenAPI docs page (`api.openapi_enabled`, off by default). ([#805](https://github.com/schubydoo/clauster/pull/805))
- Allow one standard bridge and several interactive sessions to run side by side within a single project. ([#789](https://github.com/schubydoo/clauster/pull/789))
- The Settings config surface now offers a friendly key/value row editor for the `env` map (add / edit / remove), with the raw-JSON view kept as an escape hatch for other keys. ([#879](https://github.com/schubydoo/clauster/pull/879))
- The config panel's subagent editor now treats the **Name** box as the single source of the agent's name — it's written into the frontmatter `name:` automatically on save (the backend requires the two to match), so you no longer type it twice. New agents start from a small frontmatter template that omits `name:`. ([#974](https://github.com/schubydoo/clauster/pull/974))
- Add `tls.provision = self-signed` to auto-generate and renew a self-signed HTTPS certificate at startup, stored under `state_dir/tls/`. ([#790](https://github.com/schubydoo/clauster/pull/790))
- Add `ui.enabled` (default `true`) to optionally run Clauster as a JSON-API-only deployment with no browser dashboard. ([#810](https://github.com/schubydoo/clauster/pull/810))
- Add an `uninstall.sh` / `uninstall.ps1` counterpart to the install scripts: it auto-detects the install method (standalone binary or uv/pipx/pip/scoop), removes the binary/package, the service unit, and the state directory + config, with `--dry-run`, `--keep-config`/`--keep-data`, a confirmation prompt, and fail-closed path safety. ([#881](https://github.com/schubydoo/clauster/pull/881))

### Fixes

- The dashboard now wraps its content in a single `<main>` landmark and carries a page `<h1>`, so assistive tech can navigate its regions and every content node sits inside a landmark (resolves the dashboard's remaining moderate axe landmark/heading findings). ([#963](https://github.com/schubydoo/clauster/pull/963))
- The config editor's section jump-nav now includes an Advanced chip whenever the password-gated Advanced panel is available, so the panel is discoverable without scrolling to the bottom. ([#1052](https://github.com/schubydoo/clauster/pull/1052))
- The frozen binary now completes its first-run database migration under a non-UTF-8 locale (#1015): `alembic.ini` was ASCII-cleaned so `configparser` no longer crashes on an em-dash when the binary starts with no `LANG`/`LC_ALL` against a fresh database. ([#1055](https://github.com/schubydoo/clauster/pull/1055))
- `clauster api-token issue`/`rotate`/`revoke` now accept the token label either as a positional argument or with `--label` — previously the three verbs disagreed (`issue` required `--label` while `rotate`/`revoke` took a positional), so `api-token revoke --label X` errored. ([#962](https://github.com/schubydoo/clauster/pull/962))
- `config_audit.log` is now **size-rotated** (#1011): at ~5 MB the current file becomes `config_audit.log.1`, older files shift up, and anything past 5 rotated files is dropped — so the config-write audit trail is bounded at ~30 MB instead of growing unbounded on a long-lived instance. Rotation is best-effort (a rotation error is logged and never blocks the already-best-effort audit append), so a committed config write is never held up by it. ([#1062](https://github.com/schubydoo/clauster/pull/1062))
- Docs: co-locate auth safety caveats with the options they qualify — proxy identity is admission not access control (networking + security), `auth.enabled`/`allow_unauthenticated_network` reference rows now carry their fail-closed context, and the public-API unauthenticated note states its loopback-only premise. ([#992](https://github.com/schubydoo/clauster/pull/992))
- A Background session that is not registered on claude.ai now requires a first prompt — launched blank it would idle at "send a prompt to start" forever with no way to receive one; the launch popover marks the field required and the dispatch API rejects it with a 422. ([#1053](https://github.com/schubydoo/clauster/pull/1053))
- The claustrum preflight (`doctor`, the session-start panel, and `deps list`) now resolves the binary the same way the daemon spawns it (#1013): a configured `claustrum.binary` — the documented workaround for systemd's minimal PATH — counts as present instead of a permanent false "unavailable" warning, and `deps list` agrees with `doctor` instead of reporting the same binary two different ways. ([#1057](https://github.com/schubydoo/clauster/pull/1057))
- `clauster doctor` now reports the detected claustrum daemon version and warns — advisorily, never as a failure — when it can't be confirmed at or above the release clauster pins (#1013): an unstamped/dev build or an older release is surfaced rather than silently assumed compatible, and a managed `deps/bin` install shadowed by a different `PATH`/configured binary now raises a `binary:claustrum:shadow` warning so you know which copy actually runs. Completes the claustrum-preflight follow-up (Bug 3–5). ([#1060](https://github.com/schubydoo/clauster/pull/1060))
- On Windows the claustrum daemon is now launched with `-listen-pipe`, so it opens the named-pipe listener the Windows client dials (via the `rpc.pipe` file beside the socket); POSIX keeps using the AF_UNIX socket unchanged. ([#900](https://github.com/schubydoo/clauster/pull/900))
- Teach the claustrum client to reach the daemon over a Windows named pipe (discovered via the `rpc.pipe` file beside the socket) on Windows, where asyncio cannot consume the AF_UNIX socket; POSIX keeps using the AF_UNIX socket unchanged. ([#893](https://github.com/schubydoo/clauster/pull/893))
- On Windows the claustrum daemon is now spawned with `-token-file` instead of `-token-fd 0` (a numeric fd is not a usable token handle for the Go daemon there), so clauster can start and drive a hosted-channel daemon over the named pipe. ([#902](https://github.com/schubydoo/clauster/pull/902))
- The Clone button now stays disabled until you enter a Git URL, so an empty clone no longer shows a raw error. ([#762](https://github.com/schubydoo/clauster/pull/762))
- A restart no longer hides stopped sessions: every persisted instance is now rebuilt on its own row, instead of collapsing to one card per project. ([#1118](https://github.com/schubydoo/clauster/pull/1118))
- The config editor now tells a feature switch what it needs (#1016 Part 2): a switch whose optional dependency is missing — Direct Sessions (the `claustrum` binary), notifications (`apprise`), the live-terminal view (`pyte`) — shows a "Requires … — run `clauster deps install …`" note and can't be turned on, so you learn what a feature needs at the moment you'd enable it instead of finding it silently dormant. Availability is resolved exactly the way the runtime resolves it (honoring a configured `claustrum.binary`), so a dependency that IS present never greys out its switch. ([#1064](https://github.com/schubydoo/clauster/pull/1064))
- Config-panel feedback now scrolls into view when it appears: arming a delete focuses its type-to-confirm input, and an Advanced save brings its success/error banner into the visible scrollport instead of leaving it hidden above the fold. ([#1050](https://github.com/schubydoo/clauster/pull/1050))
- Fix the config editor's section tabs stretching into full-width bars when they wrap onto multiple rows. ([#758](https://github.com/schubydoo/clauster/pull/758))
- Fix the config editor's section jumps so the heading you click lands below the sticky nav instead of hiding under it. ([#759](https://github.com/schubydoo/clauster/pull/759))
- Config panel fixes: clearing a JSON surface (Settings/Permissions/Hooks) and saving now treats a blank box as the empty default instead of rejecting it as invalid JSON, the typed scope confirm-token no longer lingers when you switch surface tabs, and the last inline `style` attributes were converted to classes so the panel no longer trips the `style-src` CSP. ([#960](https://github.com/schubydoo/clauster/pull/960))
- The config panel's "Show merged (effective) view" toggle now carries a disclosure caret and `aria-expanded` — matching the panel's other expand/collapse toggles — instead of rendering as easy-to-miss plain text. ([#975](https://github.com/schubydoo/clauster/pull/975))
- Internal: simplify effective-settings scope-merge computation to remove an unreachable branch (no behavior change). ([#826](https://github.com/schubydoo/clauster/pull/826))
- The config-management MCP, permissions, and hooks surfaces now return 404 (not 422) for an unknown scope while config-write is disabled, so a disabled surface can't be fingerprinted. ([#835](https://github.com/schubydoo/clauster/pull/835))
- The hooks config-write API now rejects plugin-owned hook commands — manage those via the plugin, not this surface. ([#817](https://github.com/schubydoo/clauster/pull/817))
- The config panel no longer rejects valid Skills and Subagents that carry forward-compatible frontmatter keys (e.g. `effort`, `license`, `metadata`), and no longer masks a benign `author` field as a secret. ([#959](https://github.com/schubydoo/clauster/pull/959))
- Broaden the automated test suite and tighten coverage checks to guard against regressions. ([#808](https://github.com/schubydoo/clauster/pull/808))
- Cross-OS robustness: reject Windows reserved device names (`CON`, `NUL`, …) as project names, honestly report a Windows background-agent stop (a hard kill, not a false clean stop), don't mislabel a transient clone-finalize failure as "already exists", preflight the AF_UNIX socket-path length for macOS, and warn when `nssm` is absent for the Windows service install. ([#917](https://github.com/schubydoo/clauster/pull/917))
- Headless CLI/MCP writers now serialize spawn/stop against the running server on a per-project cross-process lock, and state saves merge onto the store's current contents — a bridge can no longer be double-launched and a forgotten record no longer resurrects (#949). ([#951](https://github.com/schubydoo/clauster/pull/951))
- Config and CLAUDE.md writes now share one cross-process lock held on a file in the state dir, so the editor and config-write paths mutually exclude across processes without leaving a `CLAUDE.md.lock` artifact in project directories. ([#921](https://github.com/schubydoo/clauster/pull/921))
- Fix unreadable text selection in the dark theme: selected text in the config editors (and elsewhere) now renders as white on a solid Signal-Blue field (WCAG-AA 5:1) instead of Tabler's near-invisible default tint. ([#970](https://github.com/schubydoo/clauster/pull/970))
- Fix the dashboard hiding running Server Mode sessions when a project has more than one bridge. ([#1144](https://github.com/schubydoo/clauster/pull/1144))
- Close short-lived database engines' SQLite connections deterministically instead of leaving them for the garbage collector, which emitted spurious `unclosed database` warnings on Python 3.13+. ([#890](https://github.com/schubydoo/clauster/pull/890))
- Internal cleanup: share one SHA-256 hash helper across the config modules (no behavior change). ([#753](https://github.com/schubydoo/clauster/pull/753))
- The dashboard now shows a header pill when the runtime `claude` account is logged out, and `/healthz` reports its auth state (`claude_login_ok` / `claude_login_method`, covering OAuth, API-key, and helper logins) — so a stale login surfaces before a bridge silently hangs at "Starting". ([#844](https://github.com/schubydoo/clauster/pull/844))
- Completing the first-run setup wizard on a non-loopback bind now records the origin you reached it at in `auth.allowed_origins`, so the login that follows succeeds. The wizard wrote no origins at all, and `build_allowed_origins` auto-allows loopback only when the bind host is itself loopback — so on the `0.0.0.0` bind the Docker image forces, setup completed onto an empty allowlist and the very next request, the login POST, was refused with `403 origin check failed` with no way back into the dashboard short of hand-editing the generated `clauster.yml`. The origin is recorded only for a bind that auto-allows nothing, only from a submit that already cleared the wizard's own gate (the setup token, or the loopback Origin check), and only when it parses as an `http(s)` origin; a loopback install's generated config is unchanged. The bundled `compose.yaml` and the installation guide now set `CLAUSTER_AUTH_ALLOWED_ORIGINS` too, which covers the same failure on the environment-configured path that never runs the wizard. ([#1077](https://github.com/schubydoo/clauster/pull/1077))
- The first-run setup wizard now works in Docker (#1017): it writes `clauster.yml` to the persistent `/config` volume (`CLAUSTER_CONFIG`) instead of the container's ephemeral layer, and — with `CLAUSTER_SETUP_HOST=0.0.0.0` (baked into the image) — binds a reachable interface gated by a one-time token printed to the container log, so a published port can complete first-run setup without hand-writing config or an SSH tunnel. ([#1067](https://github.com/schubydoo/clauster/pull/1067))
- docs: new `reference/cli.md` catalogues every subcommand, ending the CLI scatter across installation/operations/README ([#1007](https://github.com/schubydoo/clauster/pull/1007))
- Docs: new operator-facing "How it works" page with a runtime-topology diagram, and a glossary defining the vocabulary (bridge, keeper, Server Mode / Interactive Session / Direct Session, spawn modes) the docs previously used before defining. ([#1004](https://github.com/schubydoo/clauster/pull/1004))
- docs: DB-snapshot rollback must delete stale `-wal`/`-shm` sidecars, not keep them (the snapshot is a self-contained `VACUUM INTO` copy) ([#1009](https://github.com/schubydoo/clauster/pull/1009))
- Docs: the in-app config editor gets its own guide page, the first-run setup wizard now leads the quickstart config step, and the login shepherd is discoverable from the docs home and quickstart. ([#1002](https://github.com/schubydoo/clauster/pull/1002))
- docs: installation decision table — pick an install method by platform/updates instead of reading all ten sections ([#1008](https://github.com/schubydoo/clauster/pull/1008))
- Docs accuracy: warn that the on-disk bridge log is not redacted by default (SUPPORT + operations), and replace the stale README claim that `claustrum` is not distributable with the `clauster deps install claustrum` instruction. ([#990](https://github.com/schubydoo/clauster/pull/990))
- docs: split configuration.md into a how-to guide (`guides/configuration.md`) and the generated key reference (`reference/config.md`), with a redirect from the old URL ([#1006](https://github.com/schubydoo/clauster/pull/1006))
- Docs: platform support is now a scannable capability × OS matrix on its own reference page, and the site ships an `llms.txt` map that states the bind/auth safety invariant for assistants. ([#1005](https://github.com/schubydoo/clauster/pull/1005))
- Docs site: new symptom-first Troubleshooting page, journey-grouped navigation (Start here / Concepts / Guides / Reference / Operations / Project), and the root UPGRADING / CHANGELOG / CONTRIBUTING / SUPPORT / SECURITY files now appear on the published site. ([#994](https://github.com/schubydoo/clauster/pull/994))
- Update the README and docs feature lists to describe the full dashboard Claude Code config editor (CLAUDE.md, settings/env, permissions, hooks, MCP, subagents, skills, plugins) instead of just the CLAUDE.md editor it started as. ([#883](https://github.com/schubydoo/clauster/pull/883))
- List-valued config keys can now be set from the environment, and dict-valued keys no longer advertise a variable that crashes startup. Eight `list[str]` keys — including `auth.allowed_origins`, which an auth-on deployment behind a proxy, tunnel, or non-loopback bind needs — were mapped to a `CLAUSTER_*` variable whose raw string was assigned straight into the field, so setting the documented variable failed config validation with `Input should be a valid list` and the process never started; env-only deployments (Docker/Compose in particular) had no way to set them short of hand-editing `clauster.yml`. Values now split on commas **or newlines** — the newline form so a `*_FILE` secret file can be written one entry per line, which previously collapsed into a single unusable entry — with surrounding whitespace trimmed and blank entries dropped. Note that an **empty** value now means an empty list rather than falling through to the YAML value, so `CLAUSTER_AUTH_ALLOWED_ORIGINS: ""` in a Compose file clears a configured allowlist (it fails closed, refusing origins rather than allowing them). An entry containing a literal comma — an Apprise URL in `notifications.urls` with comma-separated query targets, say — still has to be set in the YAML file. The three `dict` keys (`projects`, `claude.env`, `webhooks.events`) genuinely cannot be addressed by one variable and are now omitted from the env map entirely rather than being discoverable-but-fatal, matching what the configuration guide already documented. ([#1075](https://github.com/schubydoo/clauster/pull/1075))
- `clauster.yml.example` again shows the `config_write` and `login_shepherd` sections (with `allow_user_scope` / `allow_setup_token`) as commented-out defaults with a one-line explanation and a pointer to the config reference (#1012). The #983 declutter had dropped them, so a new operator starting from the example couldn't discover the config-management panel or the login shepherd — the surfaces the 1.0 test-plan pre-flight tells testers to enable. ([#1061](https://github.com/schubydoo/clauster/pull/1061))
- An external terminal/SSH `claude` session sharing a running bridge's directory now stays labeled external instead of being folded in as one of the bridge's managed sessions. ([#874](https://github.com/schubydoo/clauster/pull/874))
- `clauster doctor` now reports each optional extra (`pty`/`notify`) as OK or WARN with an install hint, and the dashboard's live-terminal control renders greyed with that hint when the tap is enabled but the `pyte` extra is missing instead of silently vanishing. ([#919](https://github.com/schubydoo/clauster/pull/919))
- Forgetting a stopped Interactive Session now also clears its saved pointer, so the next launch registers a fresh session instead of silently reattaching one that was archived or deleted out from under it (#671). ([#868](https://github.com/schubydoo/clauster/pull/868))
- Document that in forward-auth (header-only) mode, `trusted_ips` must list only your proxy's own IP — a broader range lets anyone reachable there bypass authentication. ([#754](https://github.com/schubydoo/clauster/pull/754))
- Config-editor validation failures now surface a per-field, plain-language message (e.g. "clone.allowed_private_cidrs: '…' does not appear to be an IPv4 or IPv6 network") instead of the raw pydantic dump with internal model names and error URLs. ([#1054](https://github.com/schubydoo/clauster/pull/1054))
- Internal frontend cleanup: dedupe shared helpers, drop dead code, and add reusable template macros (no behavior change). ([#784](https://github.com/schubydoo/clauster/pull/784))
- `clauster doctor` and the dashboard preflight panel no longer nag about the optional `apprise` dependency when notifications aren't actually going to send (#1016): the `extra:apprise` row now appears only when `notifications.enabled` is set **and** at least one `notifications.urls` entry is configured — matching when runtime actually imports apprise. (`pyte`/`pywinpty` stay ungated: pyte also reassembles the bridge connect-URL and pywinpty is the Windows Interactive-Session backend, so they matter beyond the opt-in live-terminal view. Binary deps like claustrum were already gated on their feature switch.) ([#1063](https://github.com/schubydoo/clauster/pull/1063))
- The per-launch **sandbox toggle is disabled for this release** (#1037). Evidence from the pre-RC dogfood showed `--sandbox` reached the remote-control bridge but was never passed to the server-mode session worker that runs Bash, so the security-labeled control silently did nothing — a "fail closed visibly" violation. The launch popover no longer offers it, the `sandbox` API/CLI/MCP parameter is accepted-but-inert (coerced to `default`), and any persisted `on`/`off` on a stopped card resumes safely as `default`. It returns behind dependency preflight + platform gating in #1046. ([#1059](https://github.com/schubydoo/clauster/pull/1059))
- A Direct Session's `instance_id` now persists across a clauster restart (both the JSON and DB hosted-state backends), so a client-cached id keeps resolving instead of 404ing against a freshly re-minted one. ([#843](https://github.com/schubydoo/clauster/pull/843))
- The Direct Session (hosted) lifecycle API endpoints — stop, resume, forget, and message — now accept a session's `instance_id` in addition to its `claustrum_process_id`, so an API client can address a session by whichever id it already holds. ([#840](https://github.com/schubydoo/clauster/pull/840))
- Stop hosted sessions from spawning duplicate processes when resumed twice at once, reject an unknown `permission_mode` with a clear 422, and finish pending notifications cleanly on shutdown. ([#757](https://github.com/schubydoo/clauster/pull/757))
- Fix the Windows owner-only ACL so a stuck `icacls` no longer fails the state write — it now logs a warning and proceeds on the inherited permissions, restoring the best-effort contract. ([#920](https://github.com/schubydoo/clauster/pull/920))
- Fix the CLI and MCP server reporting stale, incomplete bridge state, and bridges started from the CLI rendering as EXTERNAL/unmanaged in the dashboard: instance liveness is now persisted and read per instance rather than resolved per project, so every bridge on a project is visible to every process and can be stopped by its own id. ([#1109](https://github.com/schubydoo/clauster/pull/1109))
- A long or hostile line in a project's config file can no longer stall the dashboard's secret-redaction pass. ([#1123](https://github.com/schubydoo/clauster/pull/1123))
- Password managers no longer autofill a saved username into the launch popover's First prompt and Session name, which could otherwise become a bridge display name. ([#1122](https://github.com/schubydoo/clauster/pull/1122))
- The launch popover now closes as soon as you hit Trust & start, instead of lingering through the whole spawn. ([#761](https://github.com/schubydoo/clauster/pull/761))
- Cap the live transcript at the most recent 1000 turns, with a "showing last N" indicator, so long sessions stay responsive. ([#787](https://github.com/schubydoo/clauster/pull/787))
- An abandoned dashboard sign-in no longer wedges every later attempt: the login panel rehydrates its in-progress flow after a page reload (so Cancel is reachable), and a flow left idle for 15 minutes is reclaimed by the next sign-in. ([#1081](https://github.com/schubydoo/clauster/pull/1081))
- The dashboard's long-lived `setup-token` login now runs on Windows over a ConPTY (`pip install 'clauster[pty]'`), with the operator-pasted code redacted from returned output since a ConPTY echoes input back. ([#912](https://github.com/schubydoo/clauster/pull/912))
- The dashboard `claude setup-token` login now waits for the authorize URL to be stable across two polls before showing it, so a URL caught mid-render (split across the pty reader's chunks) is never surfaced truncated. ([#856](https://github.com/schubydoo/clauster/pull/856))
- `clauster logs` now withholds a half-flushed line until it completes, so redaction can never miss a secret split across two reads. ([#1111](https://github.com/schubydoo/clauster/pull/1111))
- The MCP Server-approvals panel now reflects approvals that live in the settings files (top-level `enabledMcpjsonServers`/`disabledMcpjsonServers`, where `claude mcp add-json --scope local|user` relocates them), so a server approved that way no longer shows as un-approved. A settings-owned approval is shown read-only (marked `settings`), since it can only be changed on the settings surface or via `claude mcp` — the panel no longer offers an approve/reject/unset that would silently revert on reload. ([#961](https://github.com/schubydoo/clauster/pull/961))
- The unapproved-MCP-server launch preflight now also treats a server decided (enabled or disabled) in a project's `.claude/settings.json` / `settings.local.json` or the user `~/.claude/settings.json` as decided — matching which sources claude actually honors — so it no longer falsely warns about a server that shows no enable gate. ([#857](https://github.com/schubydoo/clauster/pull/857))
- Launching an Interactive Session in a project with unapproved committed `.mcp.json` servers now soft-blocks with an error-styled confirm (it would otherwise hang at claude's MCP approval prompt and never connect): a "Resolve in Server approvals" button, a "Launch anyway" override, and block-by-default. Resolving the servers refreshes the check so the next launch proceeds. ([#858](https://github.com/schubydoo/clauster/pull/858))
- The per-project readiness check now warns when a committed `.mcp.json` has servers pending approval, since an interactive (pty) launch hangs invisibly at claude's MCP-approval prompt otherwise — resolve them in Server approvals first. ([#845](https://github.com/schubydoo/clauster/pull/845))
- The MCP `list_sessions` read tool no longer writes the shared state store or fires crash webhooks and notifications. ([#1110](https://github.com/schubydoo/clauster/pull/1110))
- Modals no longer close when a text-selection drag that started inside the dialog releases over the backdrop — only a click that both starts and ends on the backdrop dismisses (Escape and the close button are unchanged). ([#1049](https://github.com/schubydoo/clauster/pull/1049))
- Password managers no longer offer to fill non-credential dashboard fields (#1036). Every non-password `input`/`textarea` — config-editor rows, launch popovers, the clone URL, the first-run setup wizard — now carries the per-vendor opt-out attributes (`data-1p-ignore` / `data-lpignore` / `data-bwignore` / `data-form-type="other"`) alongside `autocomplete="off"`, which Chromium Autofill and the manager extensions ignore on its own (crbug 468153). Applied in the templates (a `{{ NO_AUTOFILL }}` global) so Alpine `x-for` row clones and the Alpine-free setup page inherit it. The login and setup **password** fields deliberately omit it, so your manager still fills/saves credentials. ([#1065](https://github.com/schubydoo/clauster/pull/1065))
- The launch popover's First prompt and custom session name fields no longer get autofilled by a password manager — they were the only opt-out inputs without an explicit `type`, which is enough for a manager to skip the opt-out entirely. ([#1095](https://github.com/schubydoo/clauster/pull/1095))
- Dismissing the first-run orientation card now collapses it to a light "No projects yet" hint instead of a near-identical block. ([#802](https://github.com/schubydoo/clauster/pull/802))
- A stopped card that shadows a live unmanaged Server Mode bridge is pruned again — the check now finds the bridge above a working session, whose own pid is the SDK worker rather than the bridge. ([#1119](https://github.com/schubydoo/clauster/pull/1119))
- Fix Stopped session cards vanishing when an unrelated `claude` happened to be running in the same project folder, and stale phantom cards never being cleaned up while another session on that project was live: the dashboard's phantom-prune now decides per instance rather than per project, and only treats an external session as a bridge when its command line actually is one (including the `--rc` form, which it previously failed to recognise). ([#1109](https://github.com/schubydoo/clauster/pull/1109))
- An Interactive Session whose preserved session was archived or deleted while stopped now surfaces as an **error** on restart — instead of a misleading "running" bridge that sits idle with no session — and its stale pointer is cleared so the next launch starts fresh (#671). This is the backstop for when the pre-launch check can't confirm the session (e.g. credentials unavailable). ([#870](https://github.com/schubydoo/clauster/pull/870))
- Fix the launch popover's consent gates squeezing their explanation text to about two words per line: Tabler's `.alert` is a flex row, so the trust, bypass-permissions and MCP-approval gates laid their explanation and controls out side by side until an explicit `display: block` restored stacking. ([#1098](https://github.com/schubydoo/clauster/pull/1098))
- Fix the black strips that appeared around the live terminal after resizing or zooming. ([#791](https://github.com/schubydoo/clauster/pull/791))
- Publish the repo's contributor conventions: `AGENTS.md` (machine-facing) + `CLAUDE.md` are now committed, and CONTRIBUTING gains the branching/review workflow (linear history, squash-merge, Greptile review). ([#991](https://github.com/schubydoo/clauster/pull/991))
- The PyPI page now renders its README links correctly (they were repo-relative, so every one 404'd there) and the package is classified `Development Status :: 5 - Production/Stable`. ([#1085](https://github.com/schubydoo/clauster/pull/1085))
- Remove the outdated "v0.3 — multi-user" line from the README; clauster is single-operator by design. ([#786](https://github.com/schubydoo/clauster/pull/786))
- Stop the environment reaper archiving a live bridge owned by another clauster instance or OS user: it filtered an account-wide environment list against an instance-scoped live set, so any bridge outside this instance's `projects_root` read as a leftover and could be archived mid-session — such an environment is now unattributable and skipped. ([#1102](https://github.com/schubydoo/clauster/pull/1102))
- The in-app restart (`POST /api/restart`) and the first-run setup wizard's post-completion restart now work on the frozen binary (#1014): `_reexec` no longer passes the binary path twice in argv (a PyInstaller one-file build has `sys.executable == sys.argv[0]`), which previously aborted with `unrecognized arguments` and left the server dead — masked only where a service manager restarts on exit. ([#1056](https://github.com/schubydoo/clauster/pull/1056))
- Internal cleanup: dedupe config-write helpers and tighten type annotations (no behavior change). ([#783](https://github.com/schubydoo/clauster/pull/783))
- Say up front when a stopped Server Mode card can't be resumed because its project already has a live bridge. ([#1151](https://github.com/schubydoo/clauster/pull/1151))
- Resuming a stopped bridge now revives the same session instead of creating a duplicate entry. ([#799](https://github.com/schubydoo/clauster/pull/799))
- The auth and web-UI guards now strip the configured `root_path` before matching routes, so path classification stays correct behind a reverse proxy that does not strip the mount prefix (defense-in-depth; the supported prefix-stripping proxy is unaffected). ([#880](https://github.com/schubydoo/clauster/pull/880))
- A blank `auth.password_hash` can no longer be logged in with. `verify_password` returned success for any *falsy* stored hash, so an empty-string hash (reachable via `password_hash: ""` or a present-but-empty `CLAUSTER_AUTH_PASSWORD_HASH`) made the source-visible dummy password a working credential for both `/login` and the `/api/reauth` step-up. Success now requires a real configured hash, and a blank or whitespace-only `password_hash` normalizes to `null` at config load — mirroring `api_token_hash` — so the value is refused at both layers. The constant-time dummy verify still runs, so no timing oracle is opened. ([#1070](https://github.com/schubydoo/clauster/pull/1070))
- An MCP server credential carried in a URL query string (`?api_key=…`), a URL fragment, or a stdio `args` element is no longer serialized into `claude mcp add-json`'s argv, where any local user could read it from `ps` / `/proc/<pid>/cmdline`. The keep-secrets-off-argv guard (`entry_needs_direct_write`) only recognized `env`/`headers` values and `scheme://user@host` URLs, which key-name redaction cannot see past; it now fails closed on a URL with a query, userinfo, or fragment component, on a non-empty `args` list, and on an unparseable URL, routing those entries to the direct (non-spawning) writer that produces identical on-disk state. A credential embedded in the URL *path* remains uncovered — every hosted-MCP URL has a path, so that needs a field-allowlist redesign. ([#1070](https://github.com/schubydoo/clauster/pull/1070))
- `clauster restore --config-out` now writes the restored `clauster.yml` atomically at mode `0600` instead of copying it at a umask-derived `0644`. The config carries the argon2 `auth.password_hash` (and `api_token_hash`), so the old behaviour published it to every local user for offline cracking — and with `--force` it actively relaxed an existing `0600` file. The write now goes through a `mkstemp` temp (created owner-only) and an atomic replace, so the hash is never briefly world-readable, a failed restore leaves no permissive file behind, and a symlink sitting at `--config-out` is replaced rather than written through to an unrelated file. The destination's parent directory is deliberately left alone, since `--config-out` is often a shared location. ([#1070](https://github.com/schubydoo/clauster/pull/1070))
- The `.bak` sibling that a local-scope settings write leaves beside `.claude/settings.local.json` is now gitignored too. Only the exact target path was ignored, so the backup — a plaintext copy of the *previous* `env` map, this surface's one secret-bearing shape — sat un-ignored next to it and could be swept into a commit by `git add -A` and pushed. `ensure_gitignored` gained an opt-in `ignore_backup_sibling` flag, set by all four writers of that file (settings, hooks, skill overrides, permissions), and those writers now run it **before** the write rather than after — so a failure between the two steps can no longer leave the secret-bearing backup trackable. ([#1070](https://github.com/schubydoo/clauster/pull/1070))
- `claude setup-token`'s authorize URL is now recovered from its OSC 8 hyperlink, so the dashboard login shepherd can read it under a Windows ConPTY where the terminal emits the URL as a hyperlink rather than plain text. ([#909](https://github.com/schubydoo/clauster/pull/909))
- Internal cleanup from a slop-cleanup sweep: share the Anthropic HTTPS transport + client base between the environments and code-sessions clients, collapse the duplicate `~/.claude.json` atomic-writer onto the shared core, and tighten a few return/param types. No behavior change. ([#873](https://github.com/schubydoo/clauster/pull/873))
- Launching an Interactive Session now pre-checks the preserved session and, if it was archived or deleted, clears the stale pointer so the launch starts fresh instead of reattaching a dead session and coming back idle (#671). Best-effort — it never blocks a launch, and leaves the pointer untouched on any uncertainty. ([#869](https://github.com/schubydoo/clauster/pull/869))
- The startup warning for an ignored `database_url` no longer cites an incorrect version. ([#835](https://github.com/schubydoo/clauster/pull/835))
- On startup, clauster now garbage-collects its own stale bridge pointers — only those that are both non-live and older than a 14-day retention window — so long-dead pointers stop accumulating (#671). A live bridge's pointer, or a recently-stopped session's (still resumable), is never touched, and any error is logged, never fatal to startup. ([#871](https://github.com/schubydoo/clauster/pull/871))
- Restore Resume on a stopped Server Mode session after a restart, and stop a declined resume reporting success. ([#1147](https://github.com/schubydoo/clauster/pull/1147))
- Fix the live transcript on a running session so turns no longer duplicate or lag, and the oldest/newest sort toggle now applies to the live feed with a matching label. ([#751](https://github.com/schubydoo/clauster/pull/751))
- Add a read-only Transcript button to the live desktop/bridge session row (hidden for worktree interactive sessions, whose transcript lives under a different working directory). ([#918](https://github.com/schubydoo/clauster/pull/918))
- The Transcripts selector (and the resume/fork picker that shares its endpoint) now opens near-instantly on unchanged transcripts (#1035): each file's `turn_count` plus the picker's label/timestamp fields are cached on a `(mtime, size)` stamp instead of re-parsing every `.jsonl` on every open — which stalled ~20 s on a project with a large transcript. Any append/change re-derives, so the listing stays correct. ([#1058](https://github.com/schubydoo/clauster/pull/1058))
- Trim `clauster.yml.example` from a dense inline-manual (247 lines, ~71% comments) to a concise quick-start template: one terse hint per key, verbose prose moved to the docs, all keys and defaults preserved. ([#882](https://github.com/schubydoo/clauster/pull/882))
- Widen the launch popover so the trust-on-start text reads comfortably instead of wrapping to a word or two per line. ([#760](https://github.com/schubydoo/clauster/pull/760))
- The README's feature catalogue is now a short summary that links to the docs site rather than restating it, and SUPPORT.md documents the release cadence (event-driven; security fixes immediate, regressions within days). ([#1083](https://github.com/schubydoo/clauster/pull/1083))
- The dashboard now offers Interactive Session on Windows when the ConPTY keeper (the `pty` extra) is installed, instead of always hiding it — the mode-picker gate was stale after Windows ConPTY support landed. ([#916](https://github.com/schubydoo/clauster/pull/916))
- Harden Windows state/config file writes: a best-effort owner-only ACL on the state dir, an in-process write lock serializing clauster's own concurrent writers, a retry over transient file-sharing violations, and LF (not CRLF) output. ([#915](https://github.com/schubydoo/clauster/pull/915))
- Interactive Session (true-resume PTY) now runs on Windows: the keeper drives the bridge over a ConPTY pseudo-console via pywinpty (`pip install 'clauster[pty]'`), falling back to Server Mode when the extra is absent. ([#903](https://github.com/schubydoo/clauster/pull/903))
- Cancelling or finishing a Windows login no longer stalls for five seconds and leaks a reader thread when the CLI leaves a child process behind. ([#1126](https://github.com/schubydoo/clauster/pull/1126))
- On Windows, stopping a poisoned bridge, a timed-out clone, or a stuck claustrum launcher now kills the whole process tree instead of leaving its children running. ([#1127](https://github.com/schubydoo/clauster/pull/1127))
- Conversations from sessions spawned in a git worktree now appear in the pty launch popover's Conversation picker (which branches a new session off a past conversation, leaving the original untouched), resolve when selected, and count toward the project's token rollup. A worktree session runs with the worktree as its cwd, so Claude files its transcript in a sibling `~/.claude/projects/<sanitized-cwd>/` directory; every reader keyed on the project root alone, so those conversations were invisible to the picker (a project whose only main-dir transcript was live showed just "New conversation") and their tokens were missing from usage. The project's own directory and its worktree siblings are now searched together. Because sanitizing a path collapses `/`, `.`, `-` and `_` all to `-`, a directory name cannot be mapped back to a path unambiguously — a sibling project named `<project>--claude-worktrees-x` produces a name carrying this project's worktree prefix — so every candidate that is a real sibling project's directory is excluded, and an unreadable projects root skips the worktree scan entirely rather than admitting an ambiguous one. The session resolver accepts the same directory set (its per-directory parent-identity check and stem validation are unchanged, so a crafted session id still cannot escape), and the liveness join counts worktree sessions too, keeping the listing and the live set in lockstep — otherwise a running worktree session would have shown as dormant and been offered in a picker meant for finished conversations, branching from one still being written. ([#1076](https://github.com/schubydoo/clauster/pull/1076))

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
