# Glossary

Short, plain-English definitions for the words the rest of the documentation
uses freely. The canonical mode vocabulary lives in
[Architecture — session modes](../architecture.md#session-modes-canonical-vocabulary);
where a term is defined there, the definition below matches it.

## Bridges and session modes

| Term | What it means | Where it's used |
| --- | --- | --- |
| **Bridge** | A `claude` process that Clauster launches in a project directory and exposes to Claude's remote-control service, so you can pick the conversation up from claude.ai or the Claude mobile app. | Everywhere; defined in [The two bridge modes](../index.md#the-two-bridge-modes). |
| **Remote control** | The Claude Code feature that lets a `claude` process running on your host be driven from `claude.ai/code`, Claude Desktop, or the mobile app. Clauster's whole job is starting and managing processes that offer it. | Product description; [Claude Code docs](https://code.claude.com/docs/en/remote-control). |
| **Attach** | Open a bridge's session from another Claude surface — `claude.ai/code`, Claude Desktop, or your phone — via the card's **Open in Claude** link or QR code. | Project cards; [Quickstart](../quickstart.md). |
| **Server Mode** (wire token `standard`) | The default bridge mode: a headless `claude remote-control` server (subcommand form) that hosts several sessions at once. The process survives a Clauster restart, but a *restarted bridge* starts with a fresh, empty context. | Launch dialog; [session modes](../architecture.md#session-modes-canonical-vocabulary). |
| **Interactive Session** (wire token `pty`) | The opt-in bridge mode: a single `claude --remote-control` session (flag form) run inside a pseudo-terminal. Because a real terminal stays attached, it genuinely restores the prior conversation after a restart (true resume, `--continue`). | `claude.launch_mode: pty`; [session modes](../architecture.md#session-modes-canonical-vocabulary). |
| **PTY keeper** | The small sidecar process that owns an Interactive Session's pseudo-terminal ("pty") and keeps it alive independently of Clauster. `clauster keepers` lists and stops orphaned ones. | [`clauster keepers`](../operations.md#clauster-keepers-stop-an-orphaned-pty-keeper). |
| **Direct Session** (channel `hosted` — a separate axis, not a third launch mode) | An experimental third way to run Claude: Clauster drives `claude` itself through the claustrum daemon and renders the conversation in its own dashboard panel. Local live-view only — never attachable from claude.ai. | The **Here in the browser** launch choice; [session modes](../architecture.md#session-modes-canonical-vocabulary). |
| **claustrum** | A separate helper daemon (not a typo of "clauster") that carries Direct Sessions. One per deployment, started automatically when `claustrum.enabled` is set; it keeps running across Clauster restarts. | [Configuration](../configuration.md). |
| **Background Agent** | A fire-and-forget `claude --bg` run that executes a task on its own and reports back. No interactive session is attached; you dispatch it and read the result. | Background-agents panel. |

## Launch options

| Term | What it means | Where it's used |
| --- | --- | --- |
| **Spawn mode** | Where a new bridge session's working directory lives. `same-dir` works in the project directory itself; `worktree` works in a separate git worktree (a linked copy of the repository — requires git); `session` works in a fresh sandbox directory. Bridge launches only — Direct Sessions ignore it. | Launch dialog; [Configuration](../configuration.md). |
| **Permission mode** | How much a session may do without asking you first: `default`, `plan`, `acceptEdits`, `auto`, `dontAsk`, or `bypassPermissions`. These are Claude Code's own modes; `bypassPermissions` (no confirmation at all) is [footgun-gated](../security.md#bypasspermissions-footgun-gate). | Launch dialog; [Configuration](../configuration.md). |
| **Workspace trust** | Claude Code's own safety gate: a directory must be marked trusted (in the runtime user's `~/.claude.json`) before a session will work in it. Clauster can write that flag before spawning so a headless bridge isn't stuck on the interactive prompt. | Green shield on project cards; [Security](../security.md#workspace-trust). |

## Host and configuration

| Term | What it means | Where it's used |
| --- | --- | --- |
| **`projects_root`** | The directory whose child directories become the project cards on the dashboard. | [Configuration](../configuration.md). |
| **`state_dir`** | Where Clauster keeps its own runtime state — the `clauster.db` database, bridge logs, and the claustrum socket. Default `~/.clauster`. | [Configuration](../configuration.md); [Privacy](../privacy.md#at-rest-inventory). |
| **Operator** | The one person who runs this Clauster instance and authenticates to it. Clauster is single-operator by design: every login and API token acts as that same person. | [Public API](../public-api.md); [Operations](../operations.md). |
| **Environment** | What a bridge registers with Claude's cloud so it becomes visible on `claude.ai/code`. A bridge that "never registers an environment" started as a process but never became reachable — the most common startup failure. | Error states; [Troubleshooting](../troubleshooting.md). |

## Identifiers

| Term | What it means | Where it's used |
| --- | --- | --- |
| **Bridge label** | The display name a bridge runs under — normally the project name. It appears on the card and names the bridge's log files (`<label>-<timestamp>-<seq>.log`). | [Reading the bridge debug log](../operations.md#reading-the-bridge-debug-log). |
| **Session ref** (`session_ref`) | A stable, non-reversible identifier for one session, used in webhook payloads so events can be correlated without exposing the secret session URL. | [Public API — lifecycle webhooks](../public-api.md#lifecycle-webhooks). |
