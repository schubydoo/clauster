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
- **Secret masking** — the Settings, MCP, Hooks, and Skills surfaces, and the
  `autoMode` box on the Permissions tab, mask secret-shaped values on read. The
  MCP surface also masks every `env` and `headers` value, the `headersHelper`
  command, and the strings in `oauth`, whatever their key names. On Settings,
  MCP, Hooks and `autoMode`, a masked value left untouched keeps the stored secret
  on write. The Skills editor masks on read but restores nothing on write, so a
  save writes the mask over the stored value. The CLAUDE.md and Subagents
  surfaces, and the `permissions` rules, return the raw content, because they
  round-trip the exact text on write. Do not put a secret anywhere in a subagent
  file, for example in an inline `mcpServers` block in the frontmatter. For what
  the Hooks tab masks, see [The Hooks tab](#the-hooks-tab). The
  Plugins surface is read-only for these values, and it masks them too. See
  [Plugins and marketplaces](#plugins-and-marketplaces).

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

### MCP server definitions

The MCP tab edits each server as one JSON object, in the same shape as an
`mcpServers` entry in `.mcp.json`. The editor accepts each field that the
[Claude Code MCP documentation](https://code.claude.com/docs/en/mcp) lists for
a transport, and each field that the installed `claude` CLI reads and keeps
for that transport even when the documentation does not list it:

| `type` | Required | Optional |
| --- | --- | --- |
| `stdio`, or no `type` | `command` | `args`, `env` |
| `http` (alias `streamable-http`), `sse` | `url` | `headers`, `headersHelper`, `oauth`, `request_timeout_ms`, `tools`, `toolPermissions`, `displayName`, `discoveryCache` |
| `ws` | `url` | `headers`, `headersHelper` |

Every transport also accepts `timeout` (milliseconds, a positive integer),
`alwaysLoad` and `bareElicitationCapability` (both `true` or `false`), and
`role` (only `"comms"`). The `oauth` object accepts `clientId`, `callbackPort`
(1 to 65535), `authServerMetadataUrl` (an `https://` URL), `scopes` (a
space-separated string) and `xaa` (`true` or `false`). On `http` and `sse`,
`tools` is a list of `{"name": …, "permission_policy": …}` objects, where the
policy is `always_allow`, `always_ask` or `always_deny`. `toolPermissions` maps
a tool name to `allow`, `ask` or `blocked`.

The editor refuses the whole entry when a field is unknown or has the wrong
type. It also refuses these values, because Claude Code refuses, skips or
ignores them:

- The `transport` key, and `env` on an `http`, `sse` or `ws` server. Earlier
  Clauster versions accepted both, but Claude Code ignores them. Use `type`
  instead of `transport`. When Clauster loads a stored server that has them, it
  converts the server (see below).

- A server name with a character other than a letter, a number, a hyphen or an
  underscore, or a name that starts with a hyphen. Earlier Clauster versions
  accepted a dot. You can still delete a stored server with a dot in its name,
  but a save that includes it is refused. Claude Code skips that server.
- A masked value (`********`) when nothing is stored behind it, for example in
  a new server that you copied from the masked view of another one. Type the
  real value instead.
- A masked value in an `http`, `sse` or `ws` server when you changed its `type`,
  or any character of its `url` or of another url field such as
  `oauth.authServerMetadataUrl`. Clauster compares the text exactly, so a
  change of letter case, an added default port or a new query string also
  counts. Clauster does not send a stored credential to a new destination.
  Type the credential again.
- A masked value in a stdio server, usually an `env` value, when you changed
  its `command` or any element of its `args`, including their order. Clauster
  compares the text exactly. It does not hand a stored value to a new command.
  Type the masked values again. A change to an unmasked `env` value keeps the
  other masked values.
- A reserved name, such as `workspace`, `claude-in-chrome` or `computer-use`.
- An entry with a `url` and no `type`. Claude Code reads such an entry as a
  stdio server and skips it.
- `"type": "sdk"`, `"sse-ide"`, `"ws-ide"` or `"claudeai-proxy"`. Claude Code
  registers these servers at runtime and refuses them in a configuration file.

When the MCP tab loads a stored server that has a field Claude Code ignores,
Clauster converts the server in the editor and shows a note above the list
that names each change. It does not change the file until you save:

- If `type` is not set, a `transport` value of `stdio`, `http`,
  `streamable-http`, `sse` or `ws` becomes `type`. If `type` is set, Clauster
  removes `transport`.
- Clauster removes `env` from an `http`, `sse` or `ws` server. Claude Code never
  read these values, so nothing is lost. The note gives the number of values.

To write the fix, open the server and save it. You do not have to edit it
first. A save through the API that still has these fields is refused, and the
error message tells you to reload the server and save it.

Clauster stores a `headersHelper` command but never runs it. Claude Code runs it
when it connects to the server. For a server in `.mcp.json` or at local scope,
Claude Code runs it only after you accept the trust dialog for that project.

An OAuth client secret is not part of the entry. Claude Code keeps it in the
system keychain or a credentials file. The `POST /api/config-write/mcp/server`
route takes an optional `client_secret` for an `http` or `sse` server. The
dashboard does not send one. Clauster passes it to
`claude mcp add-json --client-secret` through the `MCP_CLIENT_SECRET`
environment variable, never on the command line. The route refuses a
`client_secret` for an entry that holds a masked value, because Clauster then
writes the file directly and cannot deliver the secret. To change the secret of
a stored server, delete the server and add it again with every value typed in.

Three entries can look broken when they are not. The **Source** badge on each
row tells the first two apart:

- **A built-in subagent is listed with no description, and cannot be edited or
  deleted.** Claude Code compiles `general-purpose`, `explore`, `plan`,
  `claude`, `statusline-setup` and `claude-code-guide` into the CLI, so there
  is no file anywhere to edit. Every install shows these six, at both scopes,
  before you add anything of your own. You also cannot create a subagent with
  one of these names in Clauster.
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

### Subagent frontmatter checks

The Subagents editor checks the frontmatter of a subagent file before it
saves the file. It checks each field in the
[Claude Code frontmatter reference](https://code.claude.com/docs/en/sub-agents#supported-frontmatter-fields).
It accepts every value that the installed Claude Code (2.1.318) reads for
that field, also a value that the reference does not list. It refuses a value
that Claude Code rejects or ignores. If one field fails, Clauster refuses the
whole save and writes nothing.

Clauster reads YAML 1.1 and Claude Code reads YAML 1.2, so a few unquoted
spellings mean different things to the two. Clauster refuses the ones it can
detect. For example, write `true`, not `yes`. Write a plain decimal number,
not `010`, `0b101` or `1_000`. Quote a value that must stay a string.
Clauster never runs a hook command and never connects to an MCP server from
this editor.

- `name` must be lowercase letters, digits and hyphens, at most 239
  characters, and the same as the name of the file. The name is also the
  file name, so this rule is narrower than the Claude Code rule. A file with
  a longer or other name does not show in the list.
- `memory`, `color` and `experimental.cacheTtl` accept only their listed
  values, written exactly as listed.
- `effort` accepts a level name in any case, the alias `med`, or an integer.
  `isolation` accepts `worktree` and also `remote`, which Claude Code reads
  but does not document.
- `maxTurns` and `autoCompactWindow` accept a number or a string that Claude
  Code reads as a whole number, for example `200000`, `"200,000"` or `2e5`.
  `autoCompactWindow` must be from `100000` to `1000000`. Do not write
  `200k`: Claude Code reads it as 200, which is out of range.
- `background` and `omitClaudeMd` accept `true` and `false`, also as quoted
  strings. They refuse `yes`, `no`, `on` and `off`: Claude Code reads those as
  text and ignores them.
- `tools` must name at least one tool. `disallowedTools` refuses a `*` entry,
  because Claude Code then applies no denylist at all.
- `mcpServers` must be a list. Each item is the name of a server that you
  configured already, or a `name: definition` object. Claude Code reads only
  the list form and ignores a mapping.
- `permissionMode` accepts `manual` as an alias for `default`. It refuses
  `bypassPermissions`, as every config-editor surface does. See the
  [bypassPermissions footgun gate](../security.md#bypasspermissions-footgun-gate).
- A field that is not in the reference is saved as written. Claude Code
  ignores a field that it does not recognize. The one exception is `env`:
  Clauster still refuses an `env` that is not a map of names to plain values.

If you open a subagent file that writes `mcpServers` as a mapping, the editor
shows it already converted to the list form, with a note above the text. The
file on disk does not change until you save. Read the converted text, then
save to write the list form. The conversion only changes the indentation of
the `mcpServers` block, so every value and comment stays as you wrote it. If
the editor cannot convert the block safely, it shows the file as it is, and
the save tells you to rewrite `mcpServers` as a list. Two examples are a
mapping written on one line, such as `mcpServers: {github: {...}}`, and two
servers with the same name.

A file with the name of a built-in subagent, such as `claude.md`, overrides
that built-in in Claude Code. The list shows the file as a read-only row next
to the built-in row. Clauster does not edit or delete that file, so change it
on the host.

### The Hooks tab

The Hooks tab edits the `hooks` key of the settings file for the scope. It
accepts every hook event, every matcher form, every hook type, and every handler
field that the [Claude Code hooks reference](https://code.claude.com/docs/en/hooks)
documents. It also accepts the fields that the Claude Code CLI reads but the
reference does not list: `rewakeMessage`, `rewakeSummary`, and `cloud`. Any
other field for that hook type stops the save with a validation error (`422`),
and nothing is written. A `timeout` must be a number of seconds above 0, as the
CLI requires.
Clauster stores each hook as data and never runs it. It does not run a command,
send a request to a URL, call an MCP tool, or send a prompt to a model.

In Rows view, each row is one hook: the event, the matcher, the type, the main
field of that type, and the timeout.

| Type | What Claude Code does when the hook fires | Handler column |
| --- | --- | --- |
| `command` | Runs a shell command | Command |
| `http` | Sends the event data as an HTTP POST to a URL | URL |
| `mcp_tool` | Calls a tool on a configured MCP server | Server and tool |
| `prompt` | Asks a model to allow or block the action | Prompt |
| `agent` | Starts a subagent that checks a condition | Prompt |

If any hook at the scope uses another field, for example `headers`, `args`,
`async`, `model`, or an MCP tool `input`, the whole tab opens in Raw JSON. The
same happens if any value contains a line break, because a row cannot show it.

An `http` hook sends data off the host, so the tab shows where the data goes: the
scheme, the host, the port, the plain path segments, and the plain query keys of
its URL.
These parts of the URL show as `********`:

- `user:password@` credentials.
- Path segments and query keys that look like a token: a `${VAR}` reference, a
  segment of 20 or more characters, or a segment whose letters and digits number
  8 or more and mix both, with punctuation such as `;`, `:` or `=` not counted
  (for example the three segments after `/services/` in a Slack webhook).
- Every query value (for example `?token=` or `?sig=`), and the fragment.
- The whole URL, if it contains a tab or a line break, if its port is not a number,
  or if it is not a valid `http` or `https` URL.

Every non-empty `headers` value also shows as `********`, and so do
secret-shaped values in an MCP tool `input`.

If you leave a mask in place, the save keeps the stored value. These rules decide
when Clauster keeps it:

- The URL, the headers, and the `input` must be unchanged, including every visible
  part of the URL. If you change any of them, type every masked value in that hook
  again. You can change other fields, for example `timeout`, without retyping.
- If several hooks at the scope send to one destination with different masked
  values (for example two Slack webhooks), Clauster keeps them only as a set. Each
  hook keeps its own value when the hooks come back exactly as they were read, in
  any order. A save that changes any field of one of them (the `timeout` too),
  adds or removes one, or moves one to another event or matcher needs every masked
  value among them typed again.

Otherwise the save is refused (`422`) and nothing is written. These rules stop a
stored token from going to a new host or path, or into a hook that you edited. If
you edit a hook until it is identical, mask for mask, to another stored hook with
the same URL, it becomes a copy of that hook. Clauster cannot tell the text
`********` in any other hook field from a mask, so a save that holds it there is
also refused. Change that value to save.

Some secrets are not masked. The tab does not mask a `command`, its `args`, a
`prompt`, a `model`, an `if` rule, a `statusMessage`, or a `matcher`. A URL path
segment or query key shorter than 20 characters is not masked either if it has
fewer than 8 letters and digits, or only letters, or only digits. Do not put a
secret in them. For an `http` hook, put the secret in a header as a `$VAR` that
`allowedEnvVars` lists.

Clauster treats a hook that refers to `CLAUDE_PLUGIN_ROOT` in any field as a
plugin hook. While one is in the settings file, every save of the Hooks tab at
that scope is refused (`422`). Remove it in Raw JSON. To change a plugin hook,
change the plugin.

The `disableAllHooks`, `allowedHttpHookUrls`, and `httpHookAllowedEnvVars`
settings are top-level keys, not part of the `hooks` key, so the Hooks tab does
not edit them.

### Plugins and marketplaces

The Plugins tab runs the `claude plugin` command on the host for every change.
For enable, disable, install, update, uninstall, and marketplace add and
remove, Clauster passes `--scope` with the scope you chose, so one change never
reaches a second settings file. The marketplace Update button is the exception:
`claude plugin marketplace update` takes no `--scope`, because a marketplace's
checkout is shared by every scope and project. An update therefore refreshes
that marketplace for every project on the host, not only the one you selected.
The commands, flags and settings below were checked against Claude Code 2.1.318.

- **Install and update never accept a marketplace command for you.** Some
  marketplace entries install a plugin by running a command, or fetch it with a
  `headersHelper` command. Claude Code shows that command and asks before it
  runs it. Clauster never passes `-y` or `--accept-command`, so Claude Code
  refuses the install and the tab shows the error. To install such a plugin,
  read the command and run `claude plugin install` from a terminal on the host.
- **Enable and Disable can refuse a plugin that already has that state.** The
  badge on each row shows the merged state from all scopes. If the plugin is
  already enabled at the scope you chose, Claude Code refuses and names that
  scope. A plugin that the project enables cannot be disabled at user scope. To
  turn it off for yourself only, disable it at the Local scope.
- **Each marketplace row shows the source type and its location**, for example
  `github · owner/repo`. A URL that carries a user name or password, such as
  `https://user:token@host/repo.git`, shows as `********`.
- **Clauster masks credentials in what it reads.** It masks every `headers`,
  `env` and MCP server `args` value, every `headersHelper` command, and every URL
  that carries a user name or password. This applies to the marketplace list, to
  the marketplaces that a scope declares (`extraKnownMarketplaces`), and to the
  MCP server definitions in the plugin list. A masked value is never written
  back. A token in a URL query string, such as `?sig=...`, is not detected. Do
  not put a credential there.
- **The Settings tab masks `extraKnownMarketplaces` in the same way.** If you
  save the Raw JSON with a masked value unchanged, Clauster keeps the stored
  value. An `args` list shows as one `********`, so you keep it whole or type
  the full list again. Clauster refuses the save (`422`) if a masked value can no
  longer be matched to its stored value. This happens when you rename or move an
  entry or a header that holds one. It also happens when you delete, reorder or
  edit an element of a list (such as a `settings` source's `plugins`) that holds
  one. Type the real values again, then save.
- **A masked credential stays bound to its marketplace URL.** If you change any
  `url` in a marketplace entry on the Settings tab, or remove one, Clauster
  refuses the save (`422`) while that entry still holds any masked value, for
  example a `headers` value or a `headersHelper`. Any change to the text counts,
  including the letter case of the host or an added `:443`. This stops the
  stored credential from going to a new host. Type the credential again, then
  save.
- **The `additionalMarketplaces` spelling is read too.** Claude Code 2.1.232 and
  later accept it as another name for `extraKnownMarketplaces`. When a file sets
  both, the canonical key wins, as it does in Claude Code.
- **Claude Code ignores marketplaces that a project declares until you trust the
  folder.** This applies to the `extraKnownMarketplaces` entries in a project's
  `.claude/settings.json` and `.claude/settings.local.json`. Such an entry has no
  effect until the folder is trusted.

The tab does not offer these commands and settings:

- `plugin configure` and `install --config`. Plugin options can be secrets, and
  Clauster does not put a secret in a command line.
- `install --marketplace`, `marketplace add --claudeai`, `marketplace add
  --sparse`, `disable --all`, `prune`, and the authoring commands (`init`,
  `validate`, `eval`, `tag`, `test`).
- Managed policy settings such as `strictKnownMarketplaces`,
  `blockedMarketplaces` and `disableCommandPluginSources`. Only managed settings
  can hold them, and Clauster never writes managed settings. Claude Code still
  applies them: a change that the policy blocks fails with Claude Code's own
  message.

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
| A plugin install or update fails and the message names a command | The marketplace entry runs a command, and Clauster never accepts one for you | Read the command, then run `claude plugin install` from a terminal on the host |
| Enable or Disable fails with "is already enabled" or "is installed at project scope, not user" | The badge is the merged state, and the chosen scope already has that value or cannot override it | Pick the scope that the message names, or use the Local scope to override a project setting for yourself |
| Removing a marketplace returns `404` with "is not declared in user settings" | A different scope declares it | Switch to the scope that declares it and remove it there |

Need to recover an earlier config? The five most recent pre-save snapshots
sit next to the file as `clauster.yml.bak-*` — copy one back over
`clauster.yml` and restart.
