# Privacy & data at rest

Clauster sends **no telemetry, ever** — there is no analytics, crash reporting,
or usage beaconing in the code. The only outbound network traffic is the work
you ask for: cloning a repository, the gated ghost-environment reaper, and the
`claude` bridge talking to its first-party API. See
[Security](security.md) for the auth, trust, and redaction model.

That covers what *leaves* the host. This page covers what Clauster keeps **on
disk locally** — every at-rest artifact, what it contains, how long it lives,
and how to purge it. The bridge transcripts that `claude` itself writes are
included because Clauster reads them (for the cost badge) even though it does
not author them.

!!! info "Two roots"
    Almost everything Clauster writes lives under your configured **`state_dir`**
    (default `~/.clauster`). The session **transcripts** and background-agent
    state are written by the `claude` CLI under **`~/.claude/`** in the runtime
    user's home — Clauster only reads those.

## At-rest inventory

The paths below assume the default `state_dir` of `~/.clauster`; substitute your
configured value if you changed it.

| Artifact | Path | Contains | Lifetime |
| --- | --- | --- | --- |
| Persistence database | `~/.clauster/clauster.db` | The SQLite database that backs all runtime persistence: the per-bridge records, the hosted-session (claustrum live-view) records, and the session-event history described below. The same fields the legacy JSON files held — bridge label, intentional-stop flag, spawn / permission / resume modes; hosted `claude` session uuid, reattach replay cursor, log path, and project / label / permission mode. Bridge records also keep the `(pid, process-start-time)` pair the bridge last ran under — kept after the bridge stops, because that pair is what tells one project's records apart on a restart — and, while a pty bridge is running, its keeper pid. The environment id, session URLs, and status are **re-derived live** and are not persisted here. | Created on first run; stable across restarts. A stopped or crashed bridge's record is kept **until you Forget it** — nothing prunes bridge records automatically. **Forget** drops only that bridge / session record — it does not touch the session-event history (see the next row). |
| Session-event history | `~/.clauster/clauster.db` (`session_events` table) | An append-only row per bridge / session lifecycle transition: project name, mode (`standard` / `pty` / `hosted`), kind (`spawned` / `ready` / `ended` / `crashed`), and a non-reversible hashed `session_ref` grouping one session's rows. Terminal rows (`ended` / `crashed`) also carry a cumulative cost / token snapshot (`cost_usd`, input / output / cache tokens). No raw session id is stored. Powers the Projects "last used / cost" sort. | Append-only; **not** pruned or rotated, and **not** removed by **Forget** — cleared only when the parent project is deleted (cascade) or the database is reset. |
| Legacy bridge state (import source) | `~/.clauster/state.json` → `state.json.imported` | The pre-database per-project bridge store. On the first boot onto the database its rows are imported into `clauster.db` and the file is renamed `state.json.imported` (kept, not deleted). No longer written by Clauster after the import. | Present only on installs that predate the database; renamed `*.imported` once imported. |
| Legacy hosted state (import source) | `~/.clauster/hosted_state.json` → `hosted_state.json.imported` | The pre-database hosted-session store. Imported into `clauster.db` on the first database boot and renamed `hosted_state.json.imported`. No longer written after the import. | Present only on installs that predate the database; renamed `*.imported` once imported. |
| Session secret | `~/.clauster/session.secret` | The HMAC key that signs login-session cookies (`0600`). Not personal data, but a credential. | Created on first run; stable across restarts unless deleted (deleting it logs everyone out). |
| Session epoch | `~/.clauster/session.epoch` | A monotonic counter used to invalidate all sessions at once. | Persists; bumped on a global logout. |
| Config-change audit | `~/.clauster/config_audit.log` | One JSON line per in-dashboard config-write (every surface — `CLAUDE.md`, settings, permissions, hooks, MCP servers + approvals, subagents, skills, plugins, marketplaces): the surface, scope, target file, action, actor, and the top-level **key names** touched (never the values). The `CLAUDE.md` editor additionally records the byte size and a SHA-256 of the content (not the content itself); MCP-server and plugin writes additionally record which config files the change touched (each by path + SHA-256 + byte size, never the file contents) plus the `claude mcp`/`claude plugin` command it ran, recorded **shape-only** — the verb, server/plugin name, and scope, with any serialized entry reduced to its key names (every value masked). No config value is ever written to the trail. | Append-only, JSON-lines. Size-rotated by Clauster (#1011): at ~5 MB the current file becomes `config_audit.log.1`, older files shift up, and anything past 5 rotated files is dropped — so the audit trail is bounded at ~30 MB rather than growing forever. |
| Bridge debug logs | `~/.clauster/logs/<name>-<ts>-<seq>.log` | The `claude` bridge's `--debug-file` output. May contain the session deep-link URL (which embeds session / environment ids). | Rotated at `logs.bridge_log_max_size_mb` (default 10 MB); `logs.keep_rotated` rotated files kept (default 5). Whole log **sets** are also auto-pruned by `logs.retention_max_age_days` (default 30 — a spawn's set is deleted on the next spawn once its newest file is older than this), `logs.retention_max_files`, and `logs.retention_max_total_mb`. |
| Bridge stderr | `~/.clauster/logs/<name>-<ts>-<seq>.stderr.log` | The bridge's stdout/stderr (startup and controller-auth errors the `--debug-file` does not capture). | Same `logs/` directory; cleaned up alongside the bridge logs. |
| Private raw log | `~/.clauster/logs/<name>-<ts>-<seq>.raw.log` | Only written when `logs.redact_session_url: true`. The verbatim (unredacted) parse-source kept `0600`; the public `.log` becomes a redacted mirror. | Same lifetime as the bridge log it shadows. |
| PTY keeper sidecar | `~/.clauster/logs/<name>-<ts>-<seq>.keeper.json` | For **pty**-mode bridges: a small discovery file recording the bridge pid, its start time, the session id, and the `claude.ai/code` connect URL so a restarted Clauster can re-find the bridge. | Lives beside the bridge log; superseded on each new pty launch for that bridge. |
| Keeper stdout | `~/.clauster/logs/<name>-<ts>-<seq>.keeper.log` | The PTY keeper sidecar's own stdout/stderr. | Same `logs/` directory. |
| Claustrum socket + token | `~/.clauster/claustrum/daemon.sock` (and the daemon's auth token) | Present only when `claustrum.enabled` is set: the AF_UNIX socket and the token the daemon authenticates with. | Created when the daemon is spawned; the daemon is intentionally left running across Clauster restarts. |
| Session transcripts | `~/.claude/projects/<sanitized-cwd>/<uuid>.jsonl` | Written by `claude`, **not** Clauster: the full conversation transcript per session. Clauster reads these for the per-project cost / token badge. | Owned by the `claude` CLI; Clauster never deletes them. |
| Bridge pointer | `~/.claude/projects/<sanitized-cwd>/bridge-pointer.json` | A pointer `claude` writes linking a directory to its active session. | Owned by the `claude` CLI. |
| Background-agent state | `~/.claude/jobs/<id>/state.json` | Written by `claude --bg`: per background-session state the agent-view panel renders. | Owned by the `claude` CLI. |
| Background-agent roster | `~/.claude/daemon/roster.json` | Written by `claude`: the live background-worker roster (pid + start time). | Owned by the `claude` CLI. |

!!! warning "What can identify a session"
    The session and environment ids (and the deep-link URL that embeds them) act
    as **bearer-equivalent credentials** for a live session. Clauster keeps them
    out of the WebSocket log stream by default (see
    [Log redaction](security.md#log-redaction)), but they are still recorded on
    disk as operational state — in the pty keeper sidecars and, unless
    `logs.redact_session_url` is set, the on-disk bridge log. Protect them with
    `state_dir` filesystem permissions.

## How to purge

All Clauster-owned state lives under `state_dir`, so the bluntest reset is to
remove that directory while the app is **stopped**.

!!! danger "Stop Clauster first"
    Purging files out from under a running Clauster (or a live bridge) can leave
    orphaned bridge processes. Stop all bridges from the dashboard and stop the
    Clauster service before deleting anything.

- **Forget a single bridge** — the dashboard **Forget** action removes the
  bridge's record from `clauster.db`. Its log files in `~/.clauster/logs/` are
  not auto-deleted; remove them by name if you want them gone. Forget is the
  *only* thing that deletes a bridge record: a stopped or crashed one keeps its
  card across restarts until you forget it.
- **Clear all bridge logs** — delete `~/.clauster/logs/` (recreated on the next
  spawn). This also clears the keeper sidecars and stderr/raw logs. By default
  old log sets are already auto-deleted after `logs.retention_max_age_days`
  (default 30) on each new spawn, so most stale logs prune themselves.
- **Reset all login sessions** — delete `~/.clauster/session.secret` and
  `~/.clauster/session.epoch`; everyone is logged out and a fresh secret is
  minted on the next start.
- **Clear the config-change audit history** — delete
  `~/.clauster/config_audit.log` **and its rotated files**
  `~/.clauster/config_audit.log.1` … `.5` (the log is size-rotated, so history
  also lives in the numbered files — remove `config_audit.log*` to clear it all).
- **Full Clauster reset** — remove the whole `~/.clauster/` directory. Your
  projects under `projects_root` and the `claude`-owned transcripts are not
  touched.
- **Remove session transcripts** — these belong to the `claude` CLI under
  `~/.claude/projects/`. Delete the relevant `<sanitized-cwd>/` directory (or
  individual `<uuid>.jsonl` files) if you want the conversation history gone.
  Doing so also removes the data behind the cost / token badge.

After a purge, restart Clauster: a fresh `state_dir` is created on demand and
the dashboard starts from an empty state.
