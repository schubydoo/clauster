# MCP server

`clauster mcp` runs a small [Model Context Protocol](https://modelcontextprotocol.io)
server over stdio, so an MCP client — Claude Desktop, Claude Code, or any other
MCP host — can ask Clauster about its sessions through tools instead of the web
UI.

It always exposes **read** tools (`list_sessions`, `session_status`) that report
session state. It can also expose **write** tools (`spawn_session`, `stop_session`,
`resume_session`, issue #527) that drive the bridge lifecycle — but those are
**gated behind `mcp.allow_writes` and default off** (see [Read-only by
default](#read-only-by-default-mcpallow_writes) below). The server reuses the same
machinery the dashboard's `/api` routes use — the read tools see exactly what the
dashboard sees, and the write tools go through the same engine facade, so option
validation, the standard-singleton cap, and workspace-trust behave identically
headless or in the browser.

The stdio transport is **local-privileged** (reachable only by a process the
operator launched on the host), so it carries no token auth — a future
daemon-socket transport can add it. Because the surface is unauthenticated, the
write tools are gated off by default: attaching the server to an agent cannot start
or stop bridges until you opt in.

## Read-only by default (`mcp.allow_writes`)

The write tools mutate bridge state, so they are opt-in. With the default config the
server is **read-only** — `tools/list` advertises only `list_sessions` and
`session_status`, and a `spawn_session` / `stop_session` / `resume_session` call is
rejected as an unknown tool. To expose them, set:

```yaml
mcp:
  allow_writes: true
```

Then the server advertises and dispatches all five tools. The startup banner on
stderr names the active surface — `… | read-only (…)  | stdio` versus
`… | read+write (…) | stdio` — so you can see which mode a running server is in. Like
the `config_write` and `login_shepherd` gates, `mcp.allow_writes` is **not**
web-editable — set it in `clauster.yml` (file/CLI-managed only).

> **Changed in 1.0 (breaking):** the write tools shipped always-on in #950. They now
> default off; an MCP client that drove `spawn`/`stop`/`resume_session` needs
> `mcp.allow_writes: true`. See [UPGRADING](upgrading.md).

## Running it

```sh
clauster mcp -c clauster.yml
```

The server speaks newline-delimited JSON-RPC 2.0 on **stdin/stdout** and writes
only a one-line startup banner to **stderr** — stdout carries nothing but
protocol messages, as the stdio transport requires. It is a short-lived process
launched and managed by the MCP client; it exits cleanly when the client closes
its stdin.

`-c/--config` follows the same [config discovery](guides/configuration.md#loading-overrides) as every
other subcommand (`$CLAUSTER_CONFIG`, then `./clauster.yml`, then
`$CLAUSTER_HOME/clauster.yml`). A bad or missing config fails closed with a
stderr message and a non-zero exit — the server never serves against an
unresolved config.

### Wiring it into an MCP client

Point your client at the command. For example, an entry in a Claude Desktop /
Claude Code MCP config:

```json
{
  "mcpServers": {
    "clauster": {
      "command": "clauster",
      "args": ["mcp", "-c", "/path/to/clauster.yml"]
    }
  }
}
```

Run the server **as the same OS user** that runs Clauster, so it reads the same
state directory and the same `~/.claude` data (trust file, background-job state)
the service uses.

## Tools

### `list_sessions`

Lists every session Clauster can observe, as one flat array. It unions the four
session sources the dashboard already surfaces:

- **bridges** — managed `claude remote-control` bridges. A project may run
  several at once: at most one standard (Server Mode) bridge plus any number of
  Interactive Sessions, so `project` is not unique — key on `id`, which is the
  bridge's stable instance id (**changed in 1.0**: it was the project name, which
  collided across a project's bridges);
- **hosted** — Direct Session (claustrum stream-json) sessions, from their
  persisted records;
- **background-agents** — agent-view background jobs (`claude --bg`);
- **external-session** / **bridge-session** — live working sessions seen via the
  `claude agents --json` cross-check (unmanaged externals and the sessions under
  a managed bridge). A `bridge-session` is listed **once**, under the bridge that
  owns it; its `parent_instance` is that bridge's `id`.

Takes no arguments. Returns `{"count": <n>, "sessions": [...]}`. Each session
carries an `id`, a `kind`, a `status`/`state`, the owning `project` where known,
and the structural identifiers for that kind (e.g. `bridge_pid` / `keeper_pid`
for a bridge, `claustrum_process_id` for a hosted session). Only
structural/lifecycle fields are surfaced — never raw transcript or log content,
and the background-job free-text fields (already redacted upstream) are omitted
entirely.

### `session_status`

Reports one session by id.

| Argument | Type | Description |
| --- | --- | --- |
| `id` | string (required) | A session id as returned by `list_sessions` — a bridge instance id, a hosted `claustrum_process_id`, a background-agent id, or a working-session uuid. A **project name** is also accepted for a bridge, as long as that project runs exactly one; when it runs several the reply is `{"found": false, "id": ..., "ambiguous": [<bridge ids>]}` rather than an arbitrary pick. |

Returns `{"found": true, "session": {...}}` for a match, or
`{"found": false, "id": "<id>"}` for an unknown id. A blank/missing `id` comes
back as a tool error rather than a guess.

### `spawn_session`

> `spawn_session`, `stop_session`, and `resume_session` are the **write** tools —
> exposed only when [`mcp.allow_writes`](#read-only-by-default-mcpallow_writes) is on.
> On a read-only server they are not advertised and a call is rejected as unknown.

Starts a `claude` bridge for a project (the bridge channel — the same as the
dashboard's **Run Claude here**).

| Argument | Type | Description |
| --- | --- | --- |
| `project` | string (required) | The project name to start in. |
| `resume_mode` | string | `standard` (multi-session server) or `pty` (single interactive session, true-resume). |
| `spawn_mode` | string | `same-dir` · `worktree` · `session`. |
| `permission_mode` | string | The claude permission mode. |
| `custom_name` | string | Display name (standard/Server Mode only). |
| `sandbox` | string | `default` · `on` · `off` (standard only). **Disabled in this release** ([#1037](https://github.com/schubydoo/clauster/issues/1037)) — accepted but inert (coerced to `default`); the launch popover no longer offers it. Returns behind dependency-preflight + platform gating in [#1046](https://github.com/schubydoo/clauster/issues/1046). |
| `trust` | boolean | Accept the workspace-trust dialog for this project. **Defaults to `false`** — an untrusted directory is refused unless you pass `true`, the headless equivalent of the dashboard's Trust action. |

Returns `{"created": <bool>, "reason": ..., "warnings": [...], "session": {...}}`.
`created` is `false` when an already-live standard bridge was handed back instead
of launching a second (the one-per-project cap). Bad options, a forbidden
permission mode, or an untrusted directory come back as an `isError` result.

### `stop_session` / `resume_session`

Stop, or resume into its prior conversation, the bridge named by an `id` (an
instance id or a project name as returned by `list_sessions`, matched in full —
there is no prefix matching) — resolved exactly like the dashboard's DELETE /
resume routes. A project name that matches **several** bridges resolves to the
one the dashboard displays, so pass an instance id to target a specific bridge.

| Argument | Type | Description |
| --- | --- | --- |
| `id` | string (required) | The bridge id to stop / resume. |

`stop_session` returns `{"stopped": <bool>, ...}`; `resume_session` returns
`{"resumed": <bool>, ...}` — the boolean is `false` (with the id echoed back) when
no managed bridge matches. Both are **bridge channel only**; hosted-session
resume stays in the dashboard.

## Scope and safety

- **Trust is never auto-granted.** `spawn_session` defaults `trust` to `false`, so
  an MCP client cannot silently trust and execute code in an untrusted directory —
  it must pass `trust: true` deliberately, the same gate as the dashboard.
- **Fail closed.** A tool that errors is returned as an `isError` tool result,
  never a silent empty success and never a server crash.
- **No secrets on egress.** Output reuses Clauster's existing redaction and
  surfaces only structural fields. A working session's project working directory
  (`cwd`) is surfaced — the same value the dashboard shows — but bridge/hosted
  log and transcript paths, deep-link URLs, and spawn error detail are not, and
  background-job free-text is omitted entirely on top of the upstream redaction.
