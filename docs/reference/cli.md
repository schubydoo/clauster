# CLI commands

Every `clauster` subcommand in one place. Commands that read the deployment
accept `-c/--config <path>`; when it is omitted, the config is found by the
same [search order](../guides/configuration.md#loading-overrides) the server
uses (also printed by `clauster --help`).

| Command | What it does |
| --- | --- |
| [`run`](#clauster-run-start-the-server) | Start the server (the default subcommand). |
| [`hash-password`](#secret-and-token-helpers) | Hash a password for `auth.password_hash`. |
| [`hash-token`](#secret-and-token-helpers) | Mint an API token + hash for `auth.api_token_hash`. |
| [`hash-metrics-token`](#secret-and-token-helpers) | Mint a `/metrics` scrape token + hash for `observability.metrics_token_hash`. |
| [`api-token`](#clauster-api-token-named-public-api-tokens) | Issue / list / rotate / revoke named public-API bearer tokens. |
| [`doctor`](#clauster-doctor-diagnose-config-environment) | Diagnose config / environment. |
| [`backup` / `restore` / `migrate`](#clauster-backup-restore-migrate) | Back up and restore `state_dir` + config; legacy `state.json` migration. |
| [`config reconcile`](#clauster-config-reconcile-clean-up-deprecated-config-keys) | Rewrite deprecated config keys to their replacements. |
| [`install-service`](#clauster-install-service-generate-or-install-a-service-unit) | Print (or `--write`, install) a systemd / launchd / Windows service. |
| [`reap-environments`](#clauster-reap-environments-archive-ghost-environments) | Archive ghost bridge environments (dry-run by default). |
| [`keepers`](#clauster-keepers-stop-an-orphaned-pty-keeper) | List or stop orphaned pty keepers. |
| [`deps`](#clauster-deps-inspect-and-manage-optional-extras) | Inspect / side-install optional extras for the standalone binary. |
| [`projects` / `status` / `sessions` / `logs` / `open`](#headless-read-commands) | Read the deployment with no server running. |
| [`start` / `stop`](#headless-write-commands) | Spawn / stop bridges with no server running. |
| [`usage`](#clauster-usage-transcript-cost-summary) | Token + approximate cost summary for a session transcript. |
| [`mcp`](#clauster-mcp-read-only-mcp-server) | Run the read-only MCP server over stdio. |

## `clauster run` — start the server {#clauster-run-start-the-server}

`run` is the default subcommand, so bare `clauster` and `clauster -c <cfg>`
also start the server. See [Installation → Running](../installation.md#running)
for first-run guidance and
[Configuring Clauster](../guides/configuration.md) for the config file itself.

## Secret and token helpers

Three generators print a secret **once** and the hash to store in
`clauster.yml` — only the hash is ever persisted:

- `clauster hash-password` — argon2id hash for
  [`auth.password_hash`](config.md#auth-authentication-authconfig).
- `clauster hash-token` — a bearer token + SHA-256 hash for
  [`auth.api_token_hash`](config.md#auth-authentication-authconfig).
- `clauster hash-metrics-token` — a scrape token + hash for
  [`observability.metrics_token_hash`](config.md#observability-read-only-metrics-endpoint-observabilityconfig),
  so Prometheus can reach `/metrics` without a browser session
  ([Operations → scrape token](../operations.md#scrape-token-let-prometheus-in-without-a-session)).

## `clauster api-token` — named public-API tokens {#clauster-api-token-named-public-api-tokens}

`clauster api-token issue|list|rotate|revoke` manages **named** bearer tokens
for the versioned `/api/v1` surface — revocable per client, unlike the single
`auth.api_token_hash`. Full guide:
[Public API → Named API tokens](../public-api.md#named-api-tokens-clauster-api-token).

## `clauster doctor` — diagnose config / environment {#clauster-doctor-diagnose-config-environment}

Checks that `claude` is found and new enough, that `projects_root` and the
state dir are usable, and reports per-extra (`extra:<name>`) and per-binary
(`binary:<name>`) dependency rows. Run it before your first spawn and fix any
✗. Full row-by-row guide:
[Operations → `clauster doctor`](../operations.md#clauster-doctor-configuration-and-environment-diagnostics).

## `clauster backup` / `restore` / `migrate` {#clauster-backup-restore-migrate}

`backup` tars `state_dir` + the active config; `restore <archive> --state-dir …`
puts it back; `migrate` is a legacy helper for pre-database `state.json` files.
Full runbook (including corruption recovery):
[Operations → Backup, restore, and corruption recovery](../operations.md#backup-restore-and-corruption-recovery).

## `clauster config reconcile` — clean up deprecated config keys {#clauster-config-reconcile-clean-up-deprecated-config-keys}

The config schema is additive-only with back-compat aliases for renamed keys, so
a deprecated key keeps working but warns at every load and lingers in your
`clauster.yml`. `clauster config reconcile` scans the loaded config for known
deprecated keys (e.g. `claude.resume_mode` → `claude.launch_mode`,
`usage.show_cost` → `usage.mode`), explains each, and proposes the replacement key
with the equivalent value:

```sh
clauster config reconcile -c /etc/clauster/clauster.yml          # interactive
clauster config reconcile -c /etc/clauster/clauster.yml --dry-run  # preview only
clauster config reconcile -c /etc/clauster/clauster.yml --yes      # accept all
```

It rewrites the file through the same atomic backup + comment-preserving writer the
[in-app editor](../guides/config-editor.md) uses (a timestamped `.bak-*` is kept),
so your comments and formatting survive. `--dry-run` writes nothing; `--yes` applies
every proposed replacement without prompting (handy in a config-management
pipeline). A clean config is a no-op.

## `clauster install-service` — generate or install a service unit {#clauster-install-service-generate-or-install-a-service-unit}

`clauster install-service {systemd|launchd|windows}` prints a service definition
for the current deployment; add `--write` to install (and on Windows, register +
start) it. Platform walkthroughs:
[Installation → Run as a systemd service](../installation.md#run-as-a-systemd-service-linux),
and the Windows two-step under [`clauster deps`](#clauster-deps-inspect-and-manage-optional-extras)
below.

## `clauster reap-environments` — archive ghost environments {#clauster-reap-environments-archive-ghost-environments}

Archives **ghost** bridge environments (left over on the Claude side when a
bridge died without cleanup). Dry-run by default — it only prints what it
would touch until you pass `--archive` (reversible) or `--force-delete`
(hard-delete, discarding queued work). The dashboard's reaper panel is gated
behind [`reaper.ui_enabled`](config.md#reaper-ghost-environment-reaper-reaperconfig);
this CLI is always available.

**Scope: only environments inside this instance's `projects_root`.** The
environment list comes from your Anthropic account and is therefore shared by
every Clauster instance and OS user on the account, while the "which bridges are
live" check can only see this instance's own projects and this OS user's
sessions. An environment outside `projects_root` is treated as unattributable and
left alone, so running the reaper on one instance (say a dev instance on its own
`projects_root`) will not archive the live bridge of another instance whose
`projects_root` is *different*. If you have several instances, run the reaper on
each to clean up all of them.

!!! warning "Instances that share a `projects_root` are not separated by this"
    The scope check distinguishes instances by directory, so two instances
    pointed at the *same* `projects_root` can still see each other's bridges as
    leftovers — the reaper cannot tell whose they are. Give each instance its own
    `projects_root` (they also collide on project cards and pointer files
    otherwise).

## `clauster keepers` — stop an orphaned pty keeper {#clauster-keepers-stop-an-orphaned-pty-keeper}

A **pty** (true-resume) bridge runs under a detached *keeper* process that
outlives a Clauster restart. The normal stop path cleans up a keeper still
attached to a project card, but if the card is gone — its project was removed —
no dashboard row can show or stop it, leaving a live keeper (and its bridge)
running invisibly.

`clauster keepers` sweeps the keeper sidecars and surfaces those **orphans** (a
live keeper whose sidecar belongs to no current card):

```sh
clauster keepers -c /etc/clauster/clauster.yml             # list orphaned keepers
clauster keepers -c /etc/clauster/clauster.yml --kill 12345 # stop one by keeper PID
```

`--kill` refuses any PID that isn't a current orphan, so it can never take down a
keeper still attached to a card. On success it stops the keeper (and its bridge
subtree) and removes the stale sidecar.

## `clauster deps` — inspect and manage optional extras {#clauster-deps-inspect-and-manage-optional-extras}

A few capabilities live behind optional `pip` **extras** the signed standalone
binary deliberately does not bundle (`pty` → `pyte`/`pywinpty`, `notify` →
`apprise`; see [Optional extras](../installation.md#standalone-binary-no-python)).
`clauster deps` reports their status and manages a side-install beside the binary:

```sh
clauster deps list                 -c /etc/clauster/clauster.yml  # status of every extra
clauster deps install pty          -c /etc/clauster/clauster.yml  # fetch into <state_dir>/deps
clauster deps uninstall pty        -c /etc/clauster/clauster.yml  # remove it again
```

`deps list` reports each extra as **loaded** (importable now), **installed**
(present in the managed dir, pending a restart), **missing**, or **n/a** (a
platform-only extra such as `pywinpty` off Windows).

`deps install <extra>` fetches the extra's wheels into a managed `<state_dir>/deps`
directory that the standalone binary adds to its import path at startup, so the
capability loads on the next restart (a normal `pip`/`uv` install resolves extras
the usual way and doesn't need this). The standalone binary bundles `pip` so it can
run the install itself — no separate Python environment required. Because those
wheels come from PyPI and are **not** covered by the release signature, the command
prints an explicit notice and requires confirmation before downloading (`--yes` skips
the prompt); it never auto-installs. `deps uninstall <extra>` removes the extra's own
distribution from the managed dir (shared transitive dependencies are left in place).

The same mechanism also manages two **binary** dependencies, downloaded from
their pinned GitHub releases and verified against hardcoded SHA-256 checksums
into `<state_dir>/deps/bin`:

- `clauster deps install claustrum` — the
  [claustrum](https://github.com/schubydoo/claustrum) daemon that carries
  Direct Sessions (see
  [`claustrum` in the config reference](config.md#claustrum-hosted-live-view-channel-claustrumconfig)).
- `clauster deps install shawl` (Windows only) — the
  [Shawl](https://github.com/mtkennerly/shawl) service wrapper used by
  `install-service windows`.

**Installing Clauster as a Windows service is two steps, from an elevated prompt:**

```powershell
clauster deps install shawl          # 1. fetch the Shawl wrapper (once)
clauster install-service windows --write   # 2. register + start the service
```

Add `-c <path>` to either only if your config isn't in the default search location.
Like `install-service systemd/launchd --write`, `--write` on Windows *applies* the
service directly — it runs `shawl add --name Clauster … -- clauster run -c <cfg>`
(which does the `sc create`), then `sc config … start= auto` and `sc start`. It needs
Shawl from step 1 and an elevated prompt (it fails with a clear "re-run as
Administrator" if not). Omit `--write` to just print those commands for inspection.
`clauster doctor` shows a `binary:shawl` row. The Windows uninstaller (`uninstall.ps1`)
stops and deletes the `Clauster` service before removing the binary, so nothing is
left pointing at a removed executable.

## Headless read commands

Headless **read** commands drive the same engine the web UI uses, with no server
running — they are read-only and never write the running service's shared state.
`projects`, `status`, and `sessions` take `--json` for scriptable output.

```text
clauster projects                  # list discoverable projects (git / trust / bypass)
clauster status                    # list bridge instances and their status
clauster sessions                  # list live working sessions (claude agents)
clauster logs <instance> [-f]      # tail a bridge's redacted log (--follow to stream)
clauster open <instance>           # print a bridge's connect URL (--launch opens a browser)
```

`<instance>` accepts a full instance id, a **unique prefix** of one, or a project name.
The prefix form is why `clauster status` truncates the INSTANCE column to 8 characters —
what it prints is directly usable. A reference matching **several** bridges is refused and
the candidates are listed, rather than one being picked — either an id **prefix** matching
more than one bridge
([#1099](https://github.com/schubydoo/clauster/issues/1099)), or a bare **project name**
matching more than one instance
([#1150](https://github.com/schubydoo/clauster/issues/1150)). A project reaches the
two-instance state after one ordinary start → stop → start cycle (the stopped row lingers
until `forget`), so every by-name surface — the `logs`, `open`, and `stop` commands, and
the status / QR HTTP routes — refuses that name until you pass an instance id:

```console
$ clauster logs f2c456fd
clauster: ambiguous 'f2c456fd' — matches f2c456fd-aaaa-…, f2c456fd-bbbb-…; use more characters
$ clauster logs myproj    # two instances share this project name
clauster: ambiguous 'myproj' — matches f2c456fd-aaaa-…, a1b2c3d4-…; use an instance id directly
```

Both **exact** forms outrank a prefix — a full instance id first, then a project name —
and **a name that is one of your projects is never read as a prefix**, even when that
project has no bridge running. So for a hex-ish project name (`cafe`, `deadbeef`) that
happens to prefix an unrelated bridge's id, `clauster stop cafe` acts on project `cafe` or
tells you it has no bridge; it never silently stops the other one. The id stays reachable
with one more character (`cafe0`).

`logs` emits only **complete** lines. A line the bridge has half-flushed is held until
its newline arrives, so redaction always sees the whole line — a secret split across two
reads used to match no pattern in either half and print verbatim
([#1105](https://github.com/schubydoo/clauster/issues/1105)). The practical effect is that
a partially-written final line appears on the next poll under `--follow`, rather than
appearing twice in pieces.

## Headless write commands

Headless **write** commands spawn and stop bridges through that same engine —
identical mode policy, singleton cap, and option validation to the dashboard. Both
take `--json`; `start` exits non-zero on failure (`2` = bad request — unknown
project, untrusted directory, invalid option; `1` = clauster tried and could not —
bridge cap or launch failure). They are meant for driving clauster with **no
server running**, but a concurrent live server is safe: the two processes
serialize their per-project spawn/stop sections on a cross-process file lock in
the deployment state dir, the spawn re-checks the on-disk bridge record under
that lock (a bridge the server already started is handed back or reattached,
never duplicated), and every state write merges onto the store's current
contents — a record the other process added or removed in the meantime stays
added or removed. (On Windows the file lock is unavailable, so two *processes*
fall back to the atomic-write + re-detect behavior; a single process is always
fully serialized.)

```text
clauster start <project> [--mode standard|pty] [--spawn-mode …] [--permission-mode …] \
               [--name NAME] [--sandbox default|on|off] [--trust] [--json]
clauster stop  <instance> [--json]        # stop a bridge by id / unique prefix / identity
```

`<instance>` resolves the same way as for the read commands above — full id, unique
prefix, or project name — and an ambiguous reference (a prefix, or a project name with
more than one instance) is refused with the candidates listed.

`--mode` picks the bridge mode with no hidden coercion (the two modes are not
interchangeable); `--trust` accepts the workspace-trust dialog before starting
(otherwise an untrusted directory is refused). Sending a conversation turn to a
running hosted session is not a headless command — it needs the live server.

`--sandbox` is **disabled in this release** ([#1037](https://github.com/schubydoo/clauster/issues/1037)):
it is still accepted and validated, but inert (coerced to `default`) — the flag
never reached the server-mode session worker, so it silently did nothing. It
returns behind dependency-preflight + platform gating in
[#1046](https://github.com/schubydoo/clauster/issues/1046).

## `clauster usage` — transcript cost summary {#clauster-usage-transcript-cost-summary}

`clauster usage <transcript>` prints the token counts and approximate cost for
one session transcript — the same math as the dashboard's
[per-project cost badge](config.md#usage-per-project-costtoken-badge-usageconfig)
(token counts exact, dollars a ballpark from a hand-maintained price table).

## `clauster mcp` — read-only MCP server {#clauster-mcp-read-only-mcp-server}

Runs a read-only MCP server over stdio exposing project / bridge / session
reads to an MCP client. Full guide: [MCP server](../mcp.md).
