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

A bounded number carries its real range, and the editor checks that range
before it sends the save. The input's `min`/`max` attributes only advertise
the range and bound the spinner arrows — the save is a `fetch`, not a form
submit, so a value you *type* is never enforced by the browser. The editor
therefore compares it itself and refuses an out-of-range one with a message,
rather than sending it and surfacing a `422` from the full-config
re-validation. That covers **exclusive** bounds too: a timeout that must be
*greater than* zero, not *at least* zero, is refused at zero — something
`min="0"` alone cannot express.

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
- **Secret masking** — the Settings, MCP, and Skills surfaces, and the `autoMode`
  box on the Permissions tab, mask secret-shaped values on read. On Settings, MCP
  and `autoMode`, a masked value left untouched keeps the stored secret on write.
  The Skills editor masks on read but restores nothing on write, so a save writes
  the mask over the stored value. The CLAUDE.md, Hooks, and Subagents surfaces,
  and the `permissions` rules, return the raw content, because they round-trip
  the exact text on write. Do not put a secret anywhere in a subagent
  file, for example in an inline `mcpServers` block in the frontmatter.

The surface tabs:

| Tab | What it manages |
| --- | --- |
| CLAUDE.md | The scope's `CLAUDE.md` memory file, as plain text. |
| Settings | `settings.json` — friendly rows for `env` variables, with a Raw JSON escape hatch for other keys, plus a read-only merged ("effective") view showing which scope each value comes from. |
| Permissions | The `permissions` object, as rows or raw JSON, and the auto mode classifier rules (`autoMode`). See [The Permissions tab](#the-permissions-tab). |
| Hooks | The `hooks` key, as rows or raw JSON. |
| MCP | MCP server definitions (add / edit / delete), plus per-server **approvals** for committed `.mcp.json` servers — Approve / Reject each, or reset all choices behind a typed confirm. |
| Subagents | Subagent definition files: list, edit, create, delete. |
| Skills | Skills: list, a per-skill `SKILL.md` editor, create, delete. |
| Plugins | Installed plugins (install / uninstall / enable / disable / update) and the marketplaces they come from (add / remove). |

Three entries can look broken when they are not. The **Source** badge on each
row tells the first two apart:

- **A built-in subagent is listed with no description, and cannot be edited or
  deleted.** Claude Code compiles `general-purpose`, `explore` and `plan` into
  the CLI, so there is no file anywhere to edit. Every install shows these
  three, at both scopes, before you add anything of your own.
- **A plugin-provided subagent is listed, but cannot be edited or deleted.** A
  plugin owns that file, so Clauster refuses to change it. To change it, change
  the plugin. When the plugin supplies it as a symlink, Clauster never follows
  the link, so the description is blank as well.
- **A skill Clauster cannot read still appears, carrying an error in place of
  the missing information.** One unreadable skill never hides the others.

### The Permissions tab

The rows editor shows every key that the Claude Code
[settings reference](https://code.claude.com/docs/en/settings-reference#permissions)
documents for the `permissions` object:

- The rule lists `allow`, `deny` and `ask`, and the directory list
  `additionalDirectories`, which gives Claude file access outside the
  directory that the session starts in. Clauster stores each entry as you type
  it. It never parses a rule or resolves a path.
- `defaultMode`. The list offers `default`, `acceptEdits`, `plan`, `dontAsk`,
  `manual` (an alias for `default`), and `auto` at User scope only.
- Four switches. A checked box writes `true` for
  `blockReadsOutsideWorkingDirectories` and
  `blockWritesOutsideWorkingDirectories`, and `"disable"` for
  `disableBypassPermissionsMode` and `disableAutoMode`.

A shape that the rows cannot show opens in Raw JSON. A save with an unknown key
or a wrong type is refused whole, with a `422` that names the key.

`bypassPermissions` is not offered, and a save that sets it as `defaultMode` is
refused at every scope, so Clauster never writes it. If a session starts with
**No forced mode** (`inherit`), a hand-written `bypassPermissions` in User or
managed settings still takes effect. See the `inherit` caveat under the
[bypassPermissions footgun gate](../security.md#bypasspermissions-footgun-gate).

The box under the rows edits `autoMode`, the rules for the auto mode classifier.
`autoMode` is a top-level key next to `permissions`, so the Settings tab does
not show it. The box takes a JSON object with the lists `environment`, `allow`,
`soft_deny` and `hard_deny`, and the Boolean `classifyAllShell`. Put the entry
`"$defaults"` in a list to keep the built-in rules at that position. A list
without it replaces the built-in rules for that section. A `soft_deny` or
`hard_deny` list without `"$defaults"` removes the built-in blocks, including
the block for force push and the data-exfiltration rule. Run
`claude auto-mode defaults` to see the built-in rules. See
[Configure auto mode](https://code.claude.com/docs/en/auto-mode-config).

Claude Code reads `autoMode` from User settings, managed settings and the
`--settings` flag, and it reads `defaultMode: "auto"` from User and managed
settings. It ignores both in a project or local settings file, so that a
repository cannot add its own classifier rules. Clauster therefore refuses a
save of either one at Project or Local scope, and the error names User scope.
If `config_write.allow_user_scope` is off, the tab has no User scope, so edit
`~/.claude/settings.json` on the host instead. A Project or Local save with an
empty box is accepted, and it removes an `autoMode` block that is already in
that file.

If the text of an `autoMode` entry holds a secret-shaped value anywhere, the
entry shows as `********`. A secret-shaped value is a URL with a user name or
password, a `${...}` reference, or a `key: value` line with a secret-shaped key.
Leave the mask to keep the stored
entry. You can add, remove or move the other entries around a mask. If a list
still holds masks but their number no longer matches the number of masked
entries on disk, the save is refused. Restore the masks as they loaded, or type
the full entry again. If you remove every mask from a list, the save is
accepted, and it deletes those entries.

## Troubleshooting and limits

| Symptom | Why | What to do |
| --- | --- | --- |
| A field isn't in the editor | Excluded by design (secret / auth / bind / structural / code-executing) | Edit `clauster.yml` on the host; see [What it deliberately won't edit](#what-it-deliberately-wont-edit) |
| Save rejected: config changed on disk (`409`) | The file was edited externally (or by a concurrent save) after the editor loaded it | Reopen the editor and re-apply your change |
| Save refused with a validation error (`422`) | The value fails full-config validation — including the fail-closed auth check | Fix the value; the same rule would have refused it at startup |
| Advanced settings ask for a password you don't have | Step-up re-auth requires a password even when login uses another mechanism | Set one with `clauster hash-password` |
| No Advanced section / no wrench button | `config_write.enabled` is off (the whole surface 404s) | Enable it in `clauster.yml` — deliberately not web-editable |
| A saved change has no effect | Saves never live-reload | Use **Restart Clauster** in the Configuration modal |
| A subagent has no description and won't edit | It is built-in, or a plugin owns it — the **Source** badge says which | Built-in: nothing to do, there is no file. Plugin: change the plugin, not Clauster |

Need to recover an earlier config? The five most recent pre-save snapshots
sit next to the file as `clauster.yml.bak-*` — copy one back over
`clauster.yml` and restart.
