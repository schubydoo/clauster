<!-- for: 1.0.0

     Curated highlights for the release named above, folded into its notes by
     scripts/inject_highlights.py at prepare time (issue #305). Edit the body freely — it's
     the human-readable summary that leads the release. Highlights inject ONLY when the
     `for:` version matches the release being cut, so update `for:` (or empty this file)
     when you move to the next version; a stale marker safely drops the highlights rather
     than folding old ones into a new release. -->

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
- **Windows & cross-OS parity.** Interactive (true-resume PTY) sessions now run on Windows
  over ConPTY, the hosted channel speaks Windows named pipes, and state writes are hardened
  — bringing Windows to parity with POSIX across the 3-OS test matrix.
- **Drive it headless: CLI, MCP write tools, and a versioned API.** Operate Clauster
  entirely from the terminal with no server running (`clauster projects/status/sessions/
  logs/open`, `start`/`stop`), spawn/stop/resume sessions from an MCP client, and integrate
  against a versioned `/api/v1` with named, revocable bearer tokens.
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

See **Breaking Changes** below before upgrading — most notably the Docker base moved to
Alpine (derived images must use `apk`, and `su-exec` replaces `gosu`).
