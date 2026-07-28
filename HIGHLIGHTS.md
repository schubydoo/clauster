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
