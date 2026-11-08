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
  project-level scopes. Subagents have no project-local scope (Claude Code has
  no local directory for them). The Skills tab has a Local scope for
  [skill visibility](#skill-visibility) only, because Claude Code has no local
  skills folder.
- **Typed confirmation** — every document save asks you to type the scope
  name; destructive list actions (delete, reset, plugin install) have their
  own typed confirms.
- **Secret masking** — the Settings, MCP, Hooks, Subagents, and Skills
  surfaces, and the `autoMode` box and read-only view on the Permissions tab,
  mask secret-shaped values on read. The MCP surface also masks every `env`
  and `headers` value, the `headersHelper` command, and the strings in
  `oauth`, whatever their key names. It also masks the query values, the fragment and the token-like path
  segments of a server `url`. A masked value left untouched keeps the stored
  secret on write. If
  Clauster cannot match a mask to its stored value, it refuses the save and
  writes nothing. The CLAUDE.md surface and the `permissions` rules return the
  raw content, because they round-trip the exact text on write. For what the
  Subagents and Skills editors mask, see
  [Masked values in subagent and skill files](#masked-values-in-subagent-and-skill-files).
  For what the Hooks tab masks, see [The Hooks tab](#the-hooks-tab). The
  Plugins surface is read-only for these values, and it masks them too. See
  [Plugins and marketplaces](#plugins-and-marketplaces).

The surface tabs:

| Tab | What it manages |
| --- | --- |
| CLAUDE.md | The scope's `CLAUDE.md` memory file, as plain text. |
| Settings | `settings.json` — a row for each key, typed by its value, with a Raw JSON escape hatch, plus a read-only merged ("effective") view showing which scopes each value comes from. See [The Settings tab](#the-settings-tab). |
| Permissions | The `permissions` object, as rows or raw JSON, and the auto mode classifier rules (`autoMode`). See [The Permissions tab](#the-permissions-tab). |
| Hooks | The `hooks` key, as rows or raw JSON. |
| MCP | MCP server definitions (add / edit / delete), as fields or raw JSON, plus per-server **approvals** for committed `.mcp.json` servers — Approve / Reject each, or reset all choices behind a typed confirm. |
| Subagents | Subagent definition files: list, edit, create, delete. |
| Skills | Skills: list, a per-skill `SKILL.md` editor, create, delete, and the visibility of each skill (`skillOverrides`). See [Skill frontmatter checks](#skill-frontmatter-checks) and [Skill visibility](#skill-visibility). |
| Plugins | Installed plugins (install / uninstall / enable / disable / update) and the marketplaces they come from (add / remove). |

### MCP server definitions

The MCP tab edits each server in one of two modes. Use the **Fields** and
**Raw JSON** buttons above the definition to switch between them:

- **Fields** is the default. Select the `type` first. The form then shows the
  fields for that type: `command`, `args` and `env` for `stdio`, or `url`,
  `headers` and `headersHelper` for `http`, `streamable-http`, `sse` and `ws`.
  The form also shows the `oauth` fields (`clientId`, `callbackPort`,
  `authServerMetadataUrl` and `scopes`) for `http`, `streamable-http` and `sse`,
  and `timeout` and `alwaysLoad` for every type. Clauster builds the JSON
  entry from the form, so you do not write JSON for a typical server.
- **Raw JSON** shows the server as one JSON object, in the same shape as an
  `mcpServers` entry in `.mcp.json`, without the server name around it. Use it
  for the fields that the form does not show, such as `role`, `tools` or
  `oauth.xaa`. A server that has such a field opens in Raw JSON, and a note
  names the fields. A value with a line break, for example a `python -c`
  script in `args`, also opens in Raw JSON, because each field is one line. So
  does an `env` or header name that is empty or has spaces around it, because
  the form trims each name.

The switch from Fields to Raw JSON is refused while an `env` or header row has
a value but no name, or shares its name with another row. One entry cannot
hold these rows, so the switch would lose values. A note names the rows. The
same rows also keep **Save** off.

In both modes, a masked value shows as `********`. A server `url` is masked as
the [Hooks tab](#the-hooks-tab) masks an `http` hook URL: every query value, the
fragment, and each path segment or query key that looks like a token show as
`********`. The scheme, the host, the port, the plain path, and the plain query
keys stay visible, for example `https://mcp.example.com/v1?key=********`. A
`ws` or `wss` URL is masked in the same way. A URL with a user name or password,
or a `${...}` reference, shows as one `********`. A URL path segment or query
key shorter than 20 characters is not masked if it has fewer than 8 letters and
digits, or only letters, or only digits. Do not put a secret there.

If you leave the mask in place, the save keeps the stored value. A `url` that
holds a mask keeps the stored URL only when every visible character of it is
unchanged. Clauster compares the text exactly. If you change any part of such a
URL, for example the host, the path, or a query key, the save is refused
(`422`). Type the full URL again. If you change the `type` or a URL of a
remote server, or the `command` or `args` of a stdio server, the save cannot
keep a masked value (see the list below). The save also cannot keep a mask that
has no stored value behind it, for example in a row that you renamed. In both
cases, the editor names each masked value and keeps **Save** off until you
type each of these values again.

The editor accepts each field that the
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

### The Settings tab

The Settings tab edits each top-level key of the settings file for the scope,
except the keys that another tab writes. These are `permissions` and `autoMode`
(the Permissions tab), `hooks` (the Hooks tab) and `enabledPlugins` (the Plugins
tab). A save that names one of them is refused (`422`), and the error names the
tab to use. Clauster checks the shape of `env` only. It does not run or interpret
any other value. Claude Code reads the values when a session starts.

The tab has two modes, Rows and Raw JSON. In Rows mode, each top-level key of the
file is one row, and the editor follows the JSON type of the value:

- A Boolean is a switch.
- A number is a number box. A save sends a number, and an empty box blocks the
  save.
- A string is a text box. When the
  [settings reference](https://code.claude.com/docs/en/settings-reference) names
  values for the key, for example `tui` or `theme`, the box suggests them. To see
  all the suggestions, clear the box, then press the Down arrow key. You can type
  any value, and it saves as you
  typed it. A value that is not one of the known values shows a warning on its
  row and in a note at the top of the tab. The save is not refused. The known
  values also cover a custom theme (`custom:<slug>`) and a `timeFormat` pattern
  that holds a `%`.
- An object, a list or `null` shows its value, its type and an **Edit in Raw
  JSON** button. A save writes it back unchanged.
- `env` has its own table of variables below the rows.

The known values are suggestions only. Clauster copies them by hand from the
settings reference (read on 2026-11-05) and does not check them against the
installed `claude`. They can lag behind Claude Code: a value that a newer Claude
Code reads can show the warning, and a misspelled value saves too. The warning is
the only check that Clauster makes on these values.

Each value saves with the JSON type that it had when it loaded, unless you change
it. For example, the string `"1"` stays a string. To add a key, type its name in
**Add a key**, or pick one of the suggestions, then pick the type of the value:
text, true/false or number. The key suggestions are the documented keys of one
value that Claude Code reads at the current scope. When you type a documented
key, the tab selects its type for you, and you can change it. A new switch starts
off, and a new number box starts empty. A documented key that holds an object or
a list is added in Raw JSON, and the tab says so. Clauster sends any other name as
you typed it. If you type a key that another tab edits, the tab names that tab
and links to it.
The browser does not decide whether Claude Code reads a key at a scope. The server
checks every key when you save, and the error gives its reason. Raw JSON shows
the same keys as JSON. A switch between the two modes keeps every value. While a
number box is empty or two `env` variables have the same name, the tab stays in
Rows mode, and a note above the rows says why.

No other tab writes these keys in a settings file, so the Settings tab shows them:

- `enabledMcpjsonServers` and `disabledMcpjsonServers`. The MCP tab shows an
  approval from a settings file as read-only, with a `settings` badge. Change
  it here.
- `skillOverrides`. The Skills tab has no editor for it yet.
- `disabledPlugins`. Claude Code 2.1.318 does not read this key. If a file
  still has it, remove it here.

Every `env` value shows as `********`. If a hand-edited `env` is not an object,
it shows as one `********`, and the save is refused (`422`, "'env' must be an
object") until you replace it with an object in Raw JSON. The value of each
credential helper shows
as `********` too, because the command can hold the credential that it prints.
The helpers are `apiKeyHelper`, `awsAuthRefresh`, `awsCredentialExport`,
`gcpAuthRefresh`, `otelHeadersHelper` and `proxyAuthHelper`. Other values show
as `********` when they look like a secret. If you leave a mask as it is, the
save keeps the stored value. If a mask no longer matches a stored value, the
save is refused (`422`) and the settings file is not changed. This occurs when
you rename an `env` variable that shows a mask, or when you move or remove an entry of a list
that holds a mask. Type the real value again. All masks look the same, so to
change one masked entry of a list, type the real value of every masked entry in
that list.

The [settings reference](https://code.claude.com/docs/en/settings-reference)
gives each key a scope. Claude Code reads some keys only from user settings,
only from user and local settings, only from managed settings, or only from
`~/.claude.json`. Clauster refuses a save that puts such a key at a scope where
Claude Code ignores it (`422`), and the error names the scope that works. For
example:

- `skipAutoPermissionPrompt`, `modelPicker` and `spellcheck` work only at User
  scope.
- `useAutoModeDuringPlan` and `skipDangerousModePermissionPrompt` work at User
  and Local scope.
- `strictKnownMarketplaces` and `allowManagedHooksOnly` work only in managed
  settings. Clauster never writes managed settings.
- `diffTool` and `autoConnectIde` belong in `~/.claude.json`, which the Claude
  Code `/config` command writes.

If a file already holds such a key, remove the key (its **Remove** button in
Rows mode, or Raw JSON) before you save the file again at that scope. The auto
mode key `disableAutoMode` and the hook keys `disableAllHooks`,
`allowedHttpHookUrls` and `httpHookAllowedEnvVars` work in every settings file,
so the tab accepts them at every scope.

The settings reference gives `bashEditDiffEnabled` and
`sandbox.allowNestedUserNamespaces` User scope, but Claude Code also reads
`false` for them from a project or local file, so the tab accepts `false` at
every scope. Of the three files, Claude Code reads `true` for them only from
user settings, so the tab refuses `true` at Project and Local scope. If a project or local file
already holds `true` for one of them, change it to `false` or remove it before
you save. Clauster checks no other values, so it does not refuse a `mask` entry
of `sandbox.credentials.envVars` in a project file, although Claude Code drops
that entry.

Some keys are Desktop-only: only Claude Desktop reads them, and the Claude Code
CLI ignores them. The tab keeps the scope that the settings reference gives
each one. `sshConfigs` works at User scope only. `sshHostAllowlist`,
`browserExternalPageTools`, `disableBrowserExternalNavigation`,
`disableDesktopLocalSessions` and `disableMobileSimulatorTools` work only in
managed settings, so the tab refuses them at every scope. The refusal says that
only Claude Desktop reads the key. If the file that you load holds a
Desktop-only key, a note above the editor names it, and the merged view leaves
it out.

At Project and Local scope, the merged ("effective") view shows the value that
Claude Code applies from the three files. Local settings override project
settings, and project settings override user settings. Claude Code merges an
object such as `env` key by key and combines the lists from each file. A plain
value from a higher scope replaces the lower one, and `fallbackModel` comes
whole from one file. A key or value that a file is not allowed to set is left
out. When more than one file makes up a value, the **From** column names each file.
Managed settings and the `--settings` flag are not part of the view.

If a scope fails to load, the tab clears its rows. A save therefore cannot write
the values of the scope that you looked at before into the new scope.

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
refused at every scope, so Clauster never writes it. A hand-written
`bypassPermissions` in User or managed settings takes effect for a session that
starts with **No forced mode** (`inherit`). For this reason, a project offers
`inherit` only when it sets `allow_bypass_permissions: true`. See the
[bypassPermissions footgun gate](../security.md#bypasspermissions-footgun-gate).

At User scope, the box under the rows edits `autoMode`, the rules for the
auto mode classifier. `autoMode` is a top-level key next to `permissions`, so
the Settings tab does not show it. The box takes a JSON object with the lists
`environment`, `allow`, `soft_deny` and `hard_deny`, and the Boolean
`classifyAllShell`. Put the entry
`"$defaults"` in a list to keep the built-in rules at that position. A list
without it replaces the built-in rules for that section. A `soft_deny` or
`hard_deny` list without `"$defaults"` removes the built-in blocks, including
the block for force push and the data-exfiltration rule. Run
`claude auto-mode defaults` to see the built-in rules. See
[Configure auto mode](https://code.claude.com/docs/en/auto-mode-config).

Claude Code reads `autoMode` from User settings, managed settings and the
`--settings` flag, and it reads `defaultMode: "auto"` from User and managed
settings. It ignores both in a project or local settings file, so that a
repository cannot add its own classifier rules. For this reason, the tab has no
`autoMode` box at Project or Local scope:

- If the file has no `autoMode` block, the tab says that Claude Code reads
  `autoMode` only from User scope, and a **Switch to User scope** button opens
  the box there. If `config_write.allow_user_scope` is off, the tab has no User
  scope, so it tells you to edit `~/.claude/settings.json` on the host instead.
- If the file has an `autoMode` block, the tab shows it read-only, masked as
  below, and marked as ignored by Claude Code. **Remove it** marks the block for
  removal (**Keep it** undoes that). Type the scope name and press **Save** to
  remove the `autoMode` key from the file. The save leaves every other key in
  the file as it is.

A save at Project or Local scope that sends a non-empty `autoMode` or
`defaultMode: "auto"` is refused, and the error names User scope.

If the text of an `autoMode` entry holds a secret-shaped value anywhere, the
entry shows as `********`. A secret-shaped value is a URL with a user name or
password, a `${...}` reference, or a `key: value` line with a secret-shaped key.
Leave the mask to keep the stored
entry. You can add, remove or move the other entries around a mask. If a list
still holds masks but their number no longer matches the number of masked
entries on disk, the save is refused. Restore the masks as they loaded, or type
the full entry again. If you remove every mask from a list, the save is
accepted, and it deletes those entries.

### Masked values in subagent and skill files

The Subagents editor shows each secret value in the frontmatter as `********`.
Each field is masked as the tab that owns its shape masks it:

- In an inline `mcpServers` definition, every `env` and `headers` value, a
  `headersHelper` command, a URL with a user name or password, and the query
  values and token-like path segments of a `url` are masked, as on the MCP
  tab. A server name is not a secret, so a server called `oauth-gw` keeps its
  `type` and the plain parts of its `url` visible.
- In `hooks`, the parts of a hook URL and the `headers` values that the
  [Hooks tab](#the-hooks-tab) masks are masked.
- Every non-empty `env` value is masked.
- In every field, a value with a secret-shaped key, a `${...}` reference, or a
  URL with a user name or password is masked.

The Markdown body after the frontmatter is never masked or changed. Comments in
the frontmatter are not masked either. Do not put a secret in the body or in a
comment. If the frontmatter is not valid YAML, the editor cannot read its
fields. It then masks the frontmatter line by line, as the Skills editor does,
and that misses a secret under a key that does not look secret. A masked value
in such a file cannot be kept, so type the real values when you repair it. If
the file holds masked values and its frontmatter is valid YAML only after Claude
Code adds quotes to some values, the editor shows those values with the quotes,
and a save writes them with the quotes.

Clauster writes the text of a save as it receives it. Before the Subagents
editor in the dashboard sends a save, it changes each CRLF line ending to LF and
removes a byte order mark at the start of the file. After a save from the
dashboard, a file that had CRLF line endings or a byte order mark therefore has
LF line endings and no byte order mark.

If you leave a mask in place, the save keeps the stored value. The save is
refused, and nothing is written, in these cases:

- A mask with no stored value behind it, for example in a new file, under a
  renamed key, or in a server that you renamed.
- A masked value in an `http`, `sse` or `ws` server when you changed its `type`
  or any character of a URL in it. Clauster compares the URL text exactly and
  never parses it.
- A masked `env` value in a stdio server when you changed its `command` or any
  element of its `args`.
- A masked hook value when the [Hooks tab](#the-hooks-tab) rules cannot keep it.

Type the real value again to save the change. If a secret is on a value with a
YAML anchor, an alias, or a merge key, or inside an ordered map or a set, the
editor cannot mask it in place. It then shows a note and not the file. Edit
that file on the host.

The Skills editor masks each file line by line: a `key: value` line with a
secret-shaped key, also as a list item (`- token: ...`), a `${...}` reference,
or a URL with a user name or password. In a flow mapping or list, such as
`env: {API_KEY: ..., MODE: fast}`, the value of each pair with a secret-shaped
key is masked. If that value is itself a `{...}` or `[...]`, the rest of the
line shows as `********`. If a line with a secret-shaped key has its value on
the next lines (`api_key: |`, `token: &a |`, or `token:` with nothing after
it), each of those lines shows its indent and `********`. The value ends at the
first line that is not indented deeper. A blank line and a comment line that is
not indented deeper stay visible and do not end the value. After a bare
`token:`, a list item at the column of the key (a `-` followed by a space, a
tab, or the end of the line) is part of the value too. In the body of
`SKILL.md`, a bullet list under a line such as `Tokens:` therefore also shows
as `********`. The body of `SKILL.md` is masked too, because skills often hold
commands.

A secret value can also go on past the end of its line. The editor then masks
the next lines too. Each of those lines shows only its indent and `********`.
A blank line stays blank. A comment line that is
not indented deeper stays visible and does not end the value. These values go
on past their line:

- In a flow mapping or list: a value that is empty at the end of the line
  (`env: {token:`), a quoted value with no closing quote on the line, a value
  behind an anchor or a tag, a `{...}` or `[...]` value, or a plain value with
  no `,`, `}` or `]` after it. The value goes on in the lines that are indented
  deeper than its line.
- After a secret-shaped key: a quoted value with no closing quote on the line,
  or a `{...}` or `[...]` value, also behind an anchor or a tag. The value goes
  on in the lines that are indented deeper than its line. In a list item such as
  `- token: "abc`, its line starts at the `-`.
- After a secret-shaped key: a plain value, as in `token: abc`. The value goes
  on in the lines that are indented deeper than the key. A line that starts with
  a key and a `:`, with a `#`, or with a list item (a `-` and a space) is not
  part of the value, so the value ends there.

A quoted value ends with the line that closes its quote. Any other value ends at
the first line that is not indented deeper. A line of the value that is not
indented deeper is not detected and shows in clear.

In the frontmatter of `SKILL.md`, the Skills editor also masks each value that
the MCP and Hooks tabs mask, whatever its key name. In `mcpServers`, every
`headers` and `env` value and a `headersHelper` command show as `********`. For
example, `headers: {X-Custom: abc}` shows as `headers: {X-Custom: ********}`.
An `args` item is not masked by its place in the file. It shows in clear unless
the line masking above masks it, for example a `${...}` reference, a URL with a
user name or password, or a list item such as `- --token=abc`. In a flow list,
`[--token=abc]` shows in clear, as it does on the MCP tab. In `hooks`, every
`headers` value shows as `********`.
The key names stay visible, and so does an empty value. If such a value covers
more than one line, holds an escape, or has an anchor or a tag in front of it,
each line of the value shows as one `********`.

The editor also masks every `url` value in the frontmatter, for
example the `url` of a server in `mcpServers` or of an `http` hook in `hooks`.
It masks the value as the [Hooks tab](#the-hooks-tab) masks a URL. Each query
value, the fragment, and each token-like path segment show as `********`. The
scheme, the host, the port, the plain path, and the plain query keys stay
visible, for example `https://mcp.example.com/v1?key=********`. A URL with a
user name or password, or with a `${...}` reference, shows as one `********`. If
the value covers more than one line, holds an escape, or has an anchor or a tag
in front of it, each line of the value shows as one `********`.

The editor decides this view from the text of a file, not from its name. Every
file of the skill that starts with a frontmatter block gets it. So `SKILL.md`
gets it under every path that reaches it, for example `./SKILL.md` or a link,
and a copy of it gets it too. Only the frontmatter block gets the view. A `url`
line in the Markdown body, or in a later document of a YAML file, gets only the
line masks above. A save refuses (`422`) a file key that names `SKILL.md` under
another spelling or case, for example `./SKILL.md`, `skill.md` or `SKILL.md.`,
because macOS or Windows opens `SKILL.md` for it. Send the file once, as
`SKILL.md`.

Some URL text is not masked. A path segment or query key shorter than 20
characters is not masked if it has fewer than 8 letters and digits, or only
letters, or only digits. Do not put a secret in them.

If Clauster cannot read a frontmatter block the way Claude Code reads it, every
line of the block shows as `********`. This applies only to a block that holds
the text `url`, `mcpServers` or `hooks` (in any case), `http:`, `https:`,
`ws:`, `wss:` or a backslash.
A block without any of them gets only the line masks. These are the cases:

- The block is not valid YAML to Clauster.
- It holds `---` before its closing fence.
- It holds a line break that only YAML 1.1 knows.
- Only Claude Code reads its opening fence.
- It is valid only after Claude Code re-quotes it.
- It holds syntax that YAML 1.2 refuses, for example a quoted value that
  continues on a line that starts at column 0.
- It is larger than the 256 KiB file cap.

For `SKILL.md`, a save of such a file is refused, except when the frontmatter is
valid only after Claude Code re-quotes it. To change the file, type the whole
frontmatter again or edit the file on the host.

A masked line keeps its stored text on save only when every other character of
the line is unchanged. For a frontmatter `url`, the scheme, the host, the port,
the path, and every query key must be unchanged, and no query parameter can be
added. Otherwise the save is refused (`422`) and nothing is written. A kept line
must also stay where the editor masks it. A masked `url` line that you move into
the Markdown body is refused, because the body would show its token. Each stored
line is kept at most once, in order, so a
copied masked line is refused. If two stored lines look the same when masked, a
save that adds or removes one of them is refused too. A
masked line in the frontmatter is kept only while `hooks` and `mcpServers` are
unchanged. Otherwise a changed URL on another line receives the stored
credential. A new line that holds `********` is refused, so type the
real value.

A mask hides a stored value, but not its length. A refusal about a masked value
never names a size, but repeated saves near a size limit can show how long a
masked value is.

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
not `010`, `0b101` or `1_000`. Quote a value that must stay a string. See
[YAML spellings that Clauster refuses](#yaml-spellings-that-clauster-refuses).
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
  configured already, or one `name: definition` object. Claude Code reads only
  the list form and ignores a mapping. See
  [Inline MCP servers](#inline-mcp-servers) for the checks on a definition.
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

#### Inline MCP servers

Claude Code reads an inline server definition in the same shape as an entry in
`.mcp.json`. The Subagents editor therefore checks each definition with the
field rules of the MCP tab, which
[MCP server definitions](#mcp-server-definitions) describes. It accepts the
same fields for each transport. It refuses the same values, for example
`transport`, `env` on an `http`, `sse` or `ws` server, a `url` with no `type`,
and `"type": "sdk"`. The YAML spelling rules above also apply to the true or
false fields and the number fields of a definition, for example `alwaysLoad`
and `timeout`. Quote a text value that looks like a number, for example
`env: {PORT: "1e3"}` or `args: ["0o17"]`. Claude Code reads the unquoted value
as a number and skips the server, so the editor refuses it.

The name rule is not the same as on the MCP tab. Claude Code accepts any
inline server name that is not reserved, so the editor accepts a name with a
dot, a space or a leading hyphen. The editor refuses these items, because
Claude Code skips them:

- A reserved name, such as `workspace`, `claude-in-chrome`, `computer-use`,
  `Claude Preview` or `Claude Browser`.
- An item that defines more than one server. Write each server as its own
  `- name:` item.
- Two inline servers with the same name. Claude Code connects only one of them.

If an inline server in the file has `transport`, or has `env` on an `http`,
`sse` or `ws` server, the editor converts it as the MCP tab does. The note
above the text names each change. The file on disk does not change until you
save.

- If `type` is not set, a `transport` value of `stdio`, `http`,
  `streamable-http`, `sse` or `ws` becomes `type`. If `type` is set, Clauster
  removes `transport`.
- Clauster removes `env` from an `http`, `sse` or `ws` server, with the lines
  under it. Claude Code never read these values. The note gives their number.

Only those lines change, so every other value and comment stays as you wrote
it. If the editor cannot convert a server safely, it shows the file as it is,
and the save tells you what to change. An example is a definition written on
one line, such as `- srv: {transport: sse, url: ...}`.

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

In Rows view, each hook has two lines. The first line holds the event, the
matcher, and the type. The second line holds the handler inputs of the selected
type and only those, then the timeout and the Remove button.

| Type | What Claude Code does when the hook fires | Handler inputs |
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
not edit them. Edit them on [the Settings tab](#the-settings-tab).

### Plugins and marketplaces

The Plugins tab runs the `claude plugin` command on the host for every change.
For enable, disable, install, update, uninstall, and marketplace add and
remove, Clauster passes `--scope` with the scope you chose, so one change never
reaches a second settings file. The marketplace Update button is the exception:
`claude plugin marketplace update` takes no `--scope`, because a marketplace's
checkout is shared by every scope and project. An update therefore refreshes
that marketplace for every project on the host, not only the one you selected.
The commands, flags and settings below were checked against Claude Code 2.1.318.
The scope rules and the update results below were checked against Claude Code
2.1.319.

- **Each plugin row shows the scope that the plugin is installed at.** The
  plugin list holds every install on the host, at every scope, also the
  installs of other projects. Claude Code updates or uninstalls a plugin only
  at the scope (and project) where it is installed. At any other scope it
  refuses, for example with "Plugin is not installed at scope project". So the
  tab shows Update and Uninstall only on a row that is installed at the scope
  and project you are looking at.
- **Enable and Disable also work from a scope that outranks the install
  scope.** For a User-scope plugin, they work at Project and Local scope. For a
  Project-scope plugin, they work at Local scope of the same project. Claude
  Code then writes the plugin in `enabledPlugins` of that project's
  `.claude/settings.json` (Project) or `.claude/settings.local.json` (Local).
  The value overrides the lower scope for that project only, and the file of
  the install scope does not change. Claude Code refuses the other direction,
  for example a Local-scope plugin at Project scope.
- **A plugin that is installed at another scope is read-only for every other
  action.** Its row tells you where it is installed, and a link switches the
  editor to that scope (and to its project, when the project is under
  `projects_root`). The link to User scope shows only when
  `config_write.allow_user_scope` is on. A row that has no scope, or the Managed
  scope, offers no action.
- **Update says what it did.** Clauster reads the plugin list before and after
  the update. It then shows the version change ("Updated x from version 1.0.0
  to version 1.1.0."), or "already up to date" with the version. The marketplace
  Update reads the marketplace's own plugin list (the `marketplace.json` file in
  its checkout) before and after. It then shows whether that list changed, how
  many plugins the marketplace now offers, and which names were added or
  removed. If a read after the update fails, the message says that the update
  ran and that Clauster could not read the result. If the read before the update
  fails, Clauster does not run the update and shows the error.
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
  that carries a user name or password. Every other `url` is masked as the
  [Hooks tab](#the-hooks-tab) masks an `http` hook URL: each query value (for
  example `?token=` or `?sig=`), the fragment, and each path segment that looks
  like a token show as `********`. The host and the plain query keys stay
  visible. A path segment or query key under 20 characters is not masked if it
  has fewer than 8 letters and digits, or only letters, or only digits. Do not
  put a credential there. This applies to the marketplace list, to the
  marketplaces that a scope declares (`extraKnownMarketplaces`), and to the
  MCP server definitions in the plugin list. A masked value is never written
  back. A value that is not an `http`, `https`, `ws` or `wss` URL, such as
  `git@host:owner/repo.git`, shows as stored, or as one `********` if it holds
  `?` or `#`. A token in the path of such a value is not detected.
  Marketplace add refuses (`422`) a source that holds `********`, for example a
  masked URL copied from the list. Type the full URL.
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
  save. A `url` that shows a masked query value or path segment keeps the
  stored URL only when every visible character of it is unchanged. Any change to
  it, or a rename of its entry, refuses the save (`422`). Inside a list, such as
  a `settings` source's `plugins`, the element that holds the URL must also be
  unchanged and keep its place, and the list must keep its length. Type the
  full URL again.
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

### YAML spellings that Clauster refuses

These rules apply to the frontmatter of subagent files and of `SKILL.md`
files. The frontmatter is the YAML block between the two `---` lines, which
are called fences. If the frontmatter has one of these, Clauster refuses the
whole save:

- A NEL (U+0085), LINE SEPARATOR (U+2028) or PARAGRAPH SEPARATOR (U+2029)
  character. YAML 1.1 reads it as a line break, and YAML 1.2 does not, so the
  two see different fields.
- The text `---` anywhere before the closing fence, also inside a value or in
  quotes. Claude Code ends the frontmatter at the first `---`.
- An alias (a `*name` reference) to a node that contains it, such as
  `a: &a {<<: *a}`.
- A nested value that YAML 1.1 and YAML 1.2 read differently. Examples are
  `async: yes`, `timeout: 1_000` or `timeout: 010` inside `hooks`, and
  `DEBUG: yes` inside an MCP server's `env`. Other examples are `1:30`,
  `0b101`, `1e3`, `0o17`, and a tagged value such as `!!int "10"`, which the
  two read differently. Write `true`, `false` or a plain decimal. If the value
  must stay text, quote it.
- A `#` with no space before it, right after a quoted value or a bracket, such
  as `"a"#b`, `[x]#b` or `[a,#c`. YAML 1.2 refuses it, so Claude Code reads a
  changed copy of the frontmatter, or none of it. Put a space before the `#`.
- A lone carriage return (CR) that is not part of a CRLF line ending.
- A quoted value that goes on to a line that starts at column 0. Indent the
  next line.
- A key that appears twice in the same mapping. This includes two spellings
  that one YAML version reads as the same key, such as `1` and `01`, or `null`
  and `~`, and two `<<` merge keys in one mapping.
- A tab inside a plain value or after it, such as `a` then a tab then `b`.
  YAML 1.2 accepts it, but Clauster's parser does not. Use a space.

Some frontmatter is not valid YAML, but Claude Code still reads it, and so does
Clauster. When the first parse fails, Claude Code parses a changed copy of the
frontmatter. In that copy, a top-level `key: value` line gets its value in
double quotes if the key has only letters, `_` and `-`, and the value holds a
YAML symbol such as `[`, `{`, `#`, or a colon and a space. A value that is
already quoted or is a flow list such as `[Read, Grep]` stays as it is. Each
tab at the start of a line becomes two spaces. Clauster reads the same copy,
so it checks the values that Claude Code sees in the known cases. For example,
`argument-hint: [filename] [format]` saves, and its value is the text
`[filename] [format]`. The editor does not remove fields on load from a file
that Claude Code reads through this copy.

A file that is not valid YAML even after these changes is refused. One example
is a bad line that is indented under another key. Another example is a line in
a file with Windows (CRLF) line endings, because Claude Code does not change
those lines. Clauster also refuses some files that YAML 1.2 reads but its own
parser does not, because it cannot read the values the same way. The examples
are a file that uses one anchor name twice, such as `&t` on two values, an
anchor name with a dot or a non-ASCII letter, such as `&d.1`, a list item
that starts with `:`, such as `[:a]`, and a tab between a value and a comment,
such as `Bash`, a tab, then `# note`.

### What a skill save writes

A skill save writes only the files that it sends. The Skills editor sends
`SKILL.md` alone. So a save replaces `SKILL.md` and keeps every other file in
the skill directory, for example `scripts/` and reference files. The kept
files stay byte for byte as they were. Clauster removes files from a skill only
when you delete the whole skill.

On Linux and macOS, the kept files and directories also keep their modes and
their group, with these changes:

- Clauster clears the setuid and setgid bits. A directory keeps its sticky bit.
- The owner of a directory always gets read, write and search access, so that
  the next save can remove the old copy.
- The skill directory itself never gets group or world write access.
- If you are not a member of the stored group, Clauster clears the group bits.
  It never gives them to another group.

While Clauster builds the new copy, every directory in it is private to the
owner. Clauster sets the final modes only as the last step before the new copy
replaces the old one. Clauster does not keep the file owner, access control
lists, or extended attributes.

On Windows, Clauster keeps no modes. The new copy gets the access rules of the
skills directory that holds it, also while Clauster builds it.

Clauster refuses the save and writes nothing in these cases:

- The skill directory holds a symlink, or an entry that is not a regular file
  or a directory. Clauster does not follow a symlink inside the skill
  directory, and it cannot keep one without following it. Replace the symlink
  with a regular file, then save.
- Clauster cannot read a file or a directory that it must keep, or one of them
  changes while Clauster copies it.
- A sent path is a directory in the stored skill, or it is inside a path that
  the stored skill holds as a file.
- A sent path names a stored file under another spelling, and Clauster cannot
  prove which file it is. Examples are a case variant of a file that has a hard
  link, or a Windows short name such as `LONGNA~1.MD`. Send the file under its
  stored name. On macOS and Windows, a plain case variant such as `RUN.sh` for
  a stored `run.sh` replaces that file.
- The kept files and the sent files together are larger than 2 MiB, the size
  limit for one skill directory. Clauster never drops a file to make the skill
  fit.

### Skill frontmatter checks

The Skills editor checks the frontmatter of `SKILL.md` before it saves the
file. The fields are those of the
[frontmatter reference](https://code.claude.com/docs/en/skills#frontmatter-reference).
The editor accepts every value that Claude Code 2.1.318 reads for that field,
including a value that the reference does not list. Version 2.1.318 is the
version that these checks were last compared against. Clauster refuses a
value that Claude Code rejects or ignores, and it then writes nothing.
Clauster never runs a hook command or a skill script from this editor.

- Every field is optional. A `SKILL.md` without frontmatter is also valid:
  Claude Code reads the whole file as the skill's instructions. Without a
  `description`, Claude Code uses the first non-empty line of the
  instructions.
- Clauster refuses a file that opens a `---` block but never closes it.
  Clauster also refuses a fence that only Claude Code can read, for example
  a fence with a no-break space after `---`. Put each `---` alone on its
  line.
- `disable-model-invocation`, `user-invocable`, `background` and `fallback`
  accept `true` and `false`. They also accept `yes`, `no`, `on`, `off`, `1`
  and `0` in any letter case. A `user-invocable:` line with no value hides the
  skill from the `/` menu, because Claude Code reads it as `false`.
- `allowed-tools`, `disallowed-tools` and `disallowedTools` accept a space- or
  comma-separated string or a list of strings. If `disallowed-tools` is set,
  Claude Code ignores `disallowedTools`. So Clauster refuses the two
  together.
- `paths` accepts a comma-separated string or a list. `arguments` accepts a
  space-separated string or a list of names. Clauster refuses an argument
  name that is only digits.
- `argument-hint`, `when_to_use`, `agent` and `version` accept text, a number,
  `true`, `false`, or a list such as `[issue-number]`. Claude Code shows that
  list as `issue-number`. Clauster refuses a mapping. The field is
  `when_to_use`. Claude Code does not read `when-to-use`.
- `effort` accepts a level name in any case, the alias `med`, or an integer.
  It accepts these inside a one-item list too, such as `[high]`.
- `model` accepts any alias, any model ID, or `inherit`. `context` accepts
  only `fork`. `shell` accepts `bash` or `powershell` in any case.
- `name` is the command name. It can differ from the folder name and can
  hold single spaces. Clauster refuses a name that Claude Code cannot show.
  Examples are a hidden or direction-changing character, two spaces in a row,
  a space at the start or end, and `<` or `>`. The limit is 256 UTF-16 units,
  so an emoji counts as two. Clauster also refuses a name with a combining
  mark, even where Claude Code shows it.
- Clauster checks `hooks` with the same rules as the
  [Hooks tab](#the-hooks-tab) and as subagent frontmatter hooks. So it accepts
  every documented hook event, hook type and field, and it refuses any other
  value. Claude Code drops a bad entry under most events and keeps the others,
  but Clauster refuses the save instead.
- Clauster also refuses hook definitions outside `hooks`, because Claude Code
  then loads the skill with no hooks and no `allowed-tools`. A hook definition
  is a `PreToolUse` or `PermissionRequest` key, or a matcher group (a `matcher`
  and its list of `hooks` entries). Clauster looks for them at the top level
  and up to three levels into another field. It does not look into
  `metadata`, `mcpServers` or `lspServers`, because Claude Code does not.
- `metadata` must be a mapping. `created_by` and `improved_by` are fields that
  Claude Code uses for its own records, and they accept only
  `dream-proposal`. `version`, `fallback` and `disallowedTools` are not in the
  reference, but Claude Code reads them, so Clauster checks them as above.
- Clauster saves `license`, `compatibility` and every other field as written.
  The nested-value rule in
  [YAML spellings that Clauster refuses](#yaml-spellings-that-clauster-refuses)
  still applies to them.

Clauster reads YAML 1.1 and Claude Code reads YAML 1.2. If a field means
something different in the two, Clauster refuses it and names the field. For
example, `description: yes` is the text `yes` to Claude Code. `effort: 010` is
10 to Claude Code but 8 to Clauster. Quote the value, or write it as a plain
number. Clauster accepts `user-invocable: yes`, because both read it as true.

If you open a `SKILL.md` that holds a field whose value Claude Code ignores,
the editor can remove that field from the text. Examples are `shell: zsh` and
`effort: banana`. Claude Code treats such a field as if it were not there.
The removal does not change what the skill does. The editor then shows a note
above the text. The file on disk does not change until you save.

The editor removes fields only if all of these conditions are true:

- Every problem in the file is such a field.
- Each of those fields holds a single value, or it is a `metadata` value that
  is not a mapping.
- No field has a YAML tag that Clauster cannot read the same way as Claude
  Code, such as `!!bool "true"`.
- The frontmatter is a plain block of `key: value` lines, each field appears
  once, and no field is indented or comes from a `<<` merge.

If one of these is false, the editor shows the file as it is, and Clauster
refuses its save. The error names the field. Two other cases are never
removed, because Claude Code acts on them. The first is a `hooks` value that is
not a mapping and not empty: Claude Code then also drops `allowed-tools`. The
second is a `disallowed-tools` value next to `disallowedTools`: it still hides
`disallowedTools`.

### Skill visibility

The **Skill visibility** panel on the Skills tab edits the `skillOverrides` key
in the settings file of the scope: `~/.claude/settings.json` at User scope,
`.claude/settings.json` at Project scope, and `.claude/settings.local.json` at
Local scope. The key tells Claude Code how to list each skill. A skill that has
no key is `on`.

| Value | Effect in Claude Code |
| --- | --- |
| `on` | Claude Code lists the skill with its description. |
| `name-only` | Claude Code lists the skill without its description. |
| `user-invocable-only` | Claude does not see the skill. You can still run it with its `/` command. |
| `off` | Neither Claude nor you can use the skill. |

The panel shows one row for each skill folder at the scope, and one row for
each key that is already in the file. At Local scope, the rows show the skill
folders of the project. A key that matches no folder gets a **no skill folder**
badge, so you can see it and remove it. A stored value that Claude Code does
not read shows as **Not valid**. Clauster refuses a save until you choose a
value for that row.

To change the visibility of skills:

1. Open **Manage Claude Code config**, select the scope and the project, then
   open the **Skills** tab.
2. Choose a value for each row. To remove a key from the file, choose
   **No override**.
3. To set a skill that has no row, such as the bundled skill `doctor`, type its
   name in **Add a skill name**, choose a value, then select **Add**.
4. Type the scope name, then select **Save visibility**.

A save replaces the whole `skillOverrides` map with the rows that have a value.
It does not change the other keys in the settings file. If the file changed
after the panel loaded it, Clauster refuses the save and writes nothing. Close
and reopen the panel to load the file again.

A key is the name of a skill, as Claude Code shows it. The key can be a
frontmatter `name`, the name of a bundled skill such as `doctor`, or a name with
a colon such as `anthropic-skills:pdf`. Clauster refuses an empty key, the key
`__proto__`, and a name that Claude Code cannot show. The refusal names the key,
and Clauster writes nothing. The panel sends each name as you typed it, so a
space at the start or the end is part of the name.

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
| A Settings save returns `422` with "Claude Code reads … only from" | The file holds a key (or a `true` value) that Claude Code ignores at this scope | Remove the key (in Rows mode or Raw JSON), or save it at the scope that the message names. For a `true` value, set it to `false` |

Need to recover an earlier config? The five most recent pre-save snapshots
sit next to the file as `clauster.yml.bak-*` — copy one back over
`clauster.yml` and restart.
