# MCP server

`clauster mcp` runs a small [Model Context Protocol](https://modelcontextprotocol.io)
server over stdio, so an MCP client — Claude Desktop, Claude Code, or any other
MCP host — can ask Clauster about its sessions through tools instead of the web
UI.

This is the **read-only v1** (issue #527). It exposes two tools that *report*
session state and nothing that changes it: there is no spawn / stop / resume and
no token auth in this version. Those are deliberately deferred to a follow-up.
The server reuses the same read machinery the dashboard's `/api` routes use, so
it sees exactly what the dashboard sees.

## Running it

```sh
clauster mcp -c clauster.yml
```

The server speaks newline-delimited JSON-RPC 2.0 on **stdin/stdout** and writes
only a one-line startup banner to **stderr** — stdout carries nothing but
protocol messages, as the stdio transport requires. It is a short-lived process
launched and managed by the MCP client; it exits cleanly when the client closes
its stdin.

`-c/--config` follows the same [config discovery](configuration.md) as every
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

- **bridges** — managed `claude remote-control` bridges (one per project);
- **hosted** — Direct Session (claustrum stream-json) sessions, from their
  persisted records;
- **background-agents** — agent-view background jobs (`claude --bg`);
- **external-session** / **bridge-session** — live working sessions seen via the
  `claude agents --json` cross-check (unmanaged externals and the sessions under
  a managed bridge).

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
| `id` | string (required) | A session id as returned by `list_sessions` — a bridge project name, a hosted `claustrum_process_id`, a background-agent id, or a working-session uuid. |

Returns `{"found": true, "session": {...}}` for a match, or
`{"found": false, "id": "<id>"}` for an unknown id. A blank/missing `id` comes
back as a tool error rather than a guess.

## Scope and safety

- **Read-only.** No tool mutates state; `spawn` / `stop` / `resume` are not
  exposed in v1.
- **Fail closed.** A tool that errors is returned as an `isError` tool result,
  never a silent empty success and never a server crash.
- **No secrets on egress.** Output reuses Clauster's existing redaction and
  surfaces only structural fields. A working session's project working directory
  (`cwd`) is surfaced — the same value the dashboard shows — but bridge/hosted
  log and transcript paths, deep-link URLs, and spawn error detail are not, and
  background-job free-text is omitted entirely on top of the upstream redaction.
