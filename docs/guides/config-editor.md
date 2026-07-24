# The in-app config editor

Clauster's dashboard can change configuration from the browser through two
distinct surfaces, opened from buttons in the dashboard header:

- **Edit configuration** (pencil icon) — edits Clauster's own `clauster.yml`
  through a fixed, fail-closed allowlist.
- **Manage Claude Code config** (wrench icon) — manages the host's Claude Code
  configuration: CLAUDE.md, settings, permissions, hooks, MCP servers,
  subagents, skills, and plugins. This button only renders when the
  [`config_write` trust tier](../reference/config.md#config_write-code-executing-config-write-trust-tier-configwriteconfig)
  is enabled — a disabled deployment ships no dead surface.

This page is the operator's guide to both. For what every key means, see the
[configuration reference](../reference/config.md) — the field tables there are
the single home for key-by-key detail.

## Editing `clauster.yml`

The pencil button opens the **Configuration** modal. Fields are grouped into
sections — General, Claude, Instance defaults, Direct Session (live-view),
Logs, Reaper, Usage, Metrics, Observability, Notifications — with jump chips
at the top and a search box that filters by label, key, or description. Each
field renders as a typed control (toggle, bounded number, text, or a select
with human-readable option labels), with its raw YAML key shown as subtext so
you can cross-reference the file.

A switch whose feature needs an **optional dependency** that isn't installed
(Direct Sessions → the `claustrum` binary, notifications → `apprise`, the live
terminal view → `pyte`) can't be turned on and shows a "Requires … — run
`clauster deps install …`" note, so you're told what a feature needs at the
moment you'd enable it rather than discovering it dormant later. Availability is
resolved exactly the way the runtime resolves it (including a configured
`claustrum.binary`), so a dependency that *is* present never greys out its
switch.

Only the **Tier-A allowlist** of day-to-day operational fields appears here:

<!-- BEGIN GEN: editable_fields -->
| Section | Editable fields |
| --- | --- |
| `(top-level)` | `log_format` |
| `api` | `openapi_enabled` |
| `claude` | `min_version`, `agents_json_poll_interval_seconds`, `startup_grace_seconds`, `auto_enable_remote_control`, `resume_recap`, `resume_recap_max_chars`, `launch_mode`, `pty_screen_enabled` |
| `instance_defaults` | `spawn_mode`, `permission_mode`, `verbose`, `session_name_prefix`, `capacity`, `max_bridges` |
| `claustrum` | `enabled`, `socket_path`, `spawn_timeout_seconds`, `keep_children`, `request_timeout_seconds` |
| `logs` | `bridge_log_max_size_mb`, `keep_rotated`, `redact_session_url`, `strip_ansi_in_stream`, `retention_max_age_days`, `retention_max_files`, `retention_max_total_mb` |
| `reaper` | `ui_enabled` |
| `usage` | `mode`, `currency`, `currency_symbol`, `fx_rate`, `token_total_includes_cache`, `show_cost` |
| `metrics` | `enabled`, `normalize_cpu`, `show_disk`, `sample_interval_seconds`, `poll_seconds` |
| `observability` | `prometheus_enabled` |
| `notifications` | `enabled`, `browser_enabled`, `notify_on_crash`, `notify_on_ready`, `notify_on_stop`, `notify_on_permission`, `notify_on_session_end`, `notify_on_reconnect_failed` |
<!-- END GEN: editable_fields -->

The allowlist's source of truth is `EDITABLE_FIELDS` in
`src/clauster/config_editor.py` (the table above is generated from it);
`GET /api/config` returns it, so the UI only renders fields it can actually
write.

### What it deliberately won't edit

The allowlist is a structural security boundary, and that is a feature:
anything that is a secret, an auth gate, a network-bind decision, a binary
path, a structural boot setting, or a code-executing capability is excluded
and enforced fail-closed in two ways:

- **Never read back.** The editor's read API serializes only allowlisted
  values, so a secret, credential hash, or bind value never reaches the
  browser in the first place — redaction is structural, not a post-filter.
- **Never written.** A save carrying a non-allowlisted key (an `auth.*` field,
  say) is rejected with `400` at the allowlist — never silently dropped — and
  the merged result of an *allowed* edit is re-validated by constructing the
  full `ClausterConfig` before any disk I/O, so a value that fails the same
  fail-closed startup validation is refused with `422`.

Concretely, that excludes — for example — `auth.*` (passwords, tokens, the
`enabled` master switch), `host`/`port`, `projects_root`, the `projects` map,
`config_write.*` / `login_shepherd.*` (RCE / account-auth surface), the secret
URL lists (`webhooks.urls`, `notifications.urls`), auth trust lists
(`auth.allowed_origins`, `auth.reverse_proxy.trusted_ips`), and the binary
paths (`claude.binary`/`claustrum.binary`).

If you can't find a field in the editor, that is by design, not a gap. Edit
`clauster.yml` on the host (or use the CLI) instead. That same host access also
allows the corresponding comma-separated `CLAUSTER_*` environment override for
the list keys above — both require the host, which is exactly the boundary the
editor's exclusions draw.

### Advanced settings (step-up re-auth)

At the bottom of the Configuration modal, an **Advanced settings** section
exposes a second, operational-but-sensitive allowlist (Tier-B): the `clone.*`
and `webhooks.*` guardrail knobs, including rows editors for
`clone.allowed_schemes` / `clone.allowed_private_cidrs` and a
checkbox-per-event editor for `webhooks.events`. It appears only when
`config_write.enabled` is on, and saving requires you to re-enter your
password first — a short-lived "step-up" unlock, separate from your login
session. Deployments without a password set (reverse-proxy or token-only
auth) see a note instead of the unlock form; set one with
`clauster hash-password`. The Tier-B allowlist:

<!-- BEGIN GEN: tier_b_fields -->
| Section | Advanced fields |
| --- | --- |
| `clone` | `enabled`, `allow_private_hosts`, `allowed_schemes`, `allowed_private_cidrs`, `timeout_seconds`, `max_mb` |
| `webhooks` | `enabled`, `timeout_seconds`, `block_private_targets`, `events` |
<!-- END GEN: tier_b_fields -->

Its source of truth is `TIER_B_FIELDS` in the same module;
`GET`/`PUT /api/config/advanced` back the panel.

## How a save is applied

Every save of `clauster.yml` (Tier-A and Advanced alike) follows the same
fail-closed pipeline — a bad edit never reaches disk:

1. **Validate.** Disallowed keys are rejected (`400`); the merged config must
   re-validate as a full `ClausterConfig` (`422` otherwise).
2. **Lost-update guard.** If the file changed on disk since the editor loaded
   it (an external edit, or a concurrent save), the write is rejected with
   `409` instead of clobbering the newer content.
3. **Backup, then atomic replace.** The previous file is copied to a
   timestamped `clauster.yml.bak-*` (the five newest are kept), then the new
   content is written to a same-directory temp file and atomically swapped
   into place. The edit is rendered onto a comment-preserving round-trip, so
   your inline YAML comments survive.

!!! note "Saves never live-reload"
    The running process keeps its startup configuration until it restarts.
    After a save, the editor shows "Saved — restart Clauster to apply".

## Restarting from the dashboard

The Configuration modal's **Restart Clauster** button applies saved changes
without shelling into the host (#483). It re-executes the process in place:
the same PID, a re-read of the config, and — because the service unit is
never stopped — running bridges and hosted sessions survive the swap and are
reattached on startup. The confirm dialog tells you how many sessions are
running and that they keep running and reconnect; the page briefly
disconnects, polls `/healthz`, and reloads once the restarted process binds.

For restarting from the shell under systemd (and the `KillMode` caveat that
makes it safe), see
[Operations → the restart caveat](../operations.md#restart).

## Managing Claude Code config

The wrench button opens **Manage Claude Code config** — a separate modal over
the gated `/api/config-write/*` surface for the host's Claude Code
configuration (not Clauster's). It exists only when `config_write.enabled` is
on; the User scope additionally requires `config_write.allow_user_scope`.

Common mechanics across the modal:

- **Scope toggle** — User / Project / Local, with a project picker for the
  project-level scopes. Subagents and skills have no project-local scope
  (Claude Code has no local directory for them).
- **Typed confirmation** — every document save asks you to type the scope
  name; destructive list actions (delete, reset, plugin install) have their
  own typed confirms.
- **Secret masking** — secret-shaped values are masked on read and a masked
  value left untouched keeps the stored secret on write.

The surface tabs:

| Tab | What it manages |
| --- | --- |
| CLAUDE.md | The scope's `CLAUDE.md` memory file, as plain text. |
| Settings | `settings.json` — friendly rows for `env` variables, with a Raw JSON escape hatch for other keys, plus a read-only merged ("effective") view showing which scope each value comes from. |
| Permissions | Permission rules (allow / ask / deny), as rows or raw JSON. |
| Hooks | The `hooks` key, as rows or raw JSON. |
| MCP | MCP server definitions (add / edit / delete), plus per-server **approvals** for committed `.mcp.json` servers — Approve / Reject each, or reset all choices behind a typed confirm. |
| Subagents | Subagent definition files: list, edit, create, delete. |
| Skills | Skills: list, a per-skill `SKILL.md` editor, create, delete. |
| Plugins | Installed plugins (install / uninstall / enable / disable / update) and the marketplaces they come from (add / remove). |

## Troubleshooting and limits

| Symptom | Why | What to do |
| --- | --- | --- |
| A field isn't in the editor | Excluded by design (secret / auth / bind / structural / code-executing) | Edit `clauster.yml` on the host; see [What it deliberately won't edit](#what-it-deliberately-wont-edit) |
| Save rejected: config changed on disk (`409`) | The file was edited externally (or by a concurrent save) after the editor loaded it | Reopen the editor and re-apply your change |
| Save refused with a validation error (`422`) | The value fails full-config validation — including the fail-closed auth check | Fix the value; the same rule would have refused it at startup |
| Advanced settings ask for a password you don't have | Step-up re-auth requires a password even when login uses another mechanism | Set one with `clauster hash-password` |
| No Advanced section / no wrench button | `config_write.enabled` is off (the whole surface 404s) | Enable it in `clauster.yml` — deliberately not web-editable |
| A saved change has no effect | Saves never live-reload | Use **Restart Clauster** in the Configuration modal |

Need to recover an earlier config? The five most recent pre-save snapshots
sit next to the file as `clauster.yml.bak-*` — copy one back over
`clauster.yml` and restart.
