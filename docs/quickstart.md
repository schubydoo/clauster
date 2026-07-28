# Quickstart — your first bridge in a few minutes

This walks you from nothing to a running `claude` bridge you can pick up from
`claude.ai/code`, on a single machine with no authentication (loopback only). For
LAN / remote access, add auth afterwards — see [Networking](networking.md).

## Prerequisites

- **Python 3.11+**.
- The **`claude` CLI on your `PATH`**, authenticated — either logged in via
  `claude`'s interactive login **or** with `ANTHROPIC_API_KEY` set in the
  environment (either satisfies the check). Clauster *spawns* `claude` — it
  doesn't vendor it — and a spawned bridge inherits that authentication.
- A **directory that holds your projects** — each child directory becomes a card.

Install [Claude Code](https://code.claude.com/docs/en/quickstart)
and log in (a spawned bridge inherits this login):

```sh
curl -fsSL https://claude.ai/install.sh | bash     # native installer (macOS/Linux/WSL), auto-updates
# auditable alternative:  brew install --cask claude-code   # (Windows/other: see the docs link above)
claude login                                       # or: export ANTHROPIC_API_KEY=sk-ant-...
claude --version                                   # confirm it's on PATH
```

`clauster doctor` (the **Check your environment** step) confirms `claude` is found and authenticated.

```sh
mkdir -p ~/code/my-first-project    # one child dir = one project card
```

## Install

```sh
uv tool install clauster      # or: pipx install clauster / pip install clauster
```

## Write a minimal config

The fastest path is the guided one: run `clauster run` with **no** config and
Clauster serves a one-page, loopback-only **first-run setup** at
<http://127.0.0.1:7621> (override the port with `CLAUSTER_SETUP_PORT`). Enter
your projects folder, bind address, and a dashboard password; it writes a
`clauster.yml` with authentication enabled and starts up on it. Because it sets
a password, it's also the quickest way to a non-loopback (LAN) deployment —
but the wizard does not configure TLS, so before picking a LAN bind, plan to
serve over HTTPS ([built-in TLS](networking.md#native-https-built-in-tls) or a
reverse proxy); otherwise the dashboard password crosses your network in
plaintext.

Prefer to write the file by hand? The only required key is `projects_root`,
and on loopback no auth is needed:

```yaml
# clauster.yml
projects_root: ~/code
```

## Check your environment

```sh
clauster doctor
```

`doctor` confirms `claude` is found and new enough, that you're logged in, and
that `projects_root` and the state dir are usable. Fix any ✗ (FAIL) before
continuing — the most common one is "`claude` not found" (it's not on `PATH`).
Also resolve any `!` warnings such as "not logged in": these don't block the
server (`doctor` still exits 0), but a spawned bridge inherits your `claude`
login, so run a `claude` session first. (You can also fix a logged-out runtime
account later from the browser with the opt-in
[login shepherd](reference/config.md#login_shepherd-dashboard-driven-claude-account-login-loginshepherdconfig)
panel — handy when the account expires after setup.)

## Run it

```sh
clauster run -c clauster.yml
```

Open **<http://127.0.0.1:7621>**. You'll see one card per child directory of
`projects_root` — including `my-first-project`.

!!! tip "Empty dashboard or a page that won't load?"
    - **"No projects yet"** means `projects_root` resolved to a directory with no
      child folders — create or clone one from the dashboard, or drop a directory
      under `projects_root` and refresh. Re-run `clauster doctor` to
      confirm `projects_root` points where you think.
    - **The page won't load from another machine** because, by default, Clauster
      binds loopback (`127.0.0.1`) and is reachable only from the host itself. A
      non-loopback bind (a LAN IP, `0.0.0.0`) is **refused unless auth is
      enforced** — that is deliberate. To go beyond loopback, add auth and follow
      [Networking](networking.md).

## Start your first bridge

On the project card, click **Run Claude here**, choose **In claude.ai /
Desktop** (the bridge this quickstart walks through), pick a permission mode,
then click **Run ·** `<permission>` (the button names the mode you picked).

- If the directory isn't trusted yet, Clauster shows a **just-in-time trust
  confirm**. Accepting writes the Claude workspace-trust flag for that directory
  *before* the bridge spawns; a trusted directory shows a green shield and starts
  with no prompt. (Trust is Claude's own safety gate — see [Security](security.md).)
- The card moves to **starting**, then **running** once the bridge registers.
- The same popover offers an experimental **Here in the browser** option (only
  when the hosted channel is enabled — `claustrum.enabled`, default off). Those
  sessions are local live-view only and, unlike a bridge, are never attachable
  from `claude.ai/code` — see [Architecture](architecture.md) and
  [Configuration](reference/config.md).

!!! note "First-ever spawn"
    Before the first spawn, Clauster marks remote control acknowledged in your
    `~/.claude.json` so the detached bridge isn't stuck on Claude's one-time
    "Enable Remote Control? (y/n)" prompt. This is `claude.auto_enable_remote_control`
    (on by default).

## Pick it up from anywhere

On the running card, use **Open in Claude** (or scan the **QR code**) to
attach the bridge from `claude.ai/code` or the Claude mobile app — no SSH session
required. Drive the session there; the live debug-log tail and resource metrics
stay visible on the card.

## Stop / resume

- **Stop** ends the bridge.
- **Resume** brings a stopped bridge back. In the default *Server Mode* a
  resume starts a fresh context; opt into the *Interactive Session* mode with
  `claude.launch_mode: pty`, or recap the prior conversation with
  `claude.resume_recap`. See [the two bridge modes](index.md#the-two-bridge-modes).
  Resume stays available across a clauster restart. One Server Mode bridge runs per
  project at a time, so a stopped card whose project already has a live bridge says so
  on the card and its Resume does nothing — stop the live one first, or **Forget** the
  stopped card. *Interactive Sessions* are not capped this way.
- **Fork a past conversation** — launching an *Interactive Session*, the
  **Conversation** picker (under *More options*) lists the project's previous
  conversations by their first prompt. Picking one forks it into the new
  session (`--resume --fork-session` under the hood): the new session continues
  from that conversation, and the original stays untouched.
- To start fresh, **Forget** the stopped session (drops it from Recent) and
  launch again with **Run Claude here**.

## Next steps

- **Expose it on your LAN / remotely** — a non-loopback bind *requires* enforced
  auth. Generate a hash with `clauster hash-password` and follow
  [Networking](networking.md).
- **Run it as a service** — `clauster install-service {systemd|launchd|windows}`.
- **Run it in Docker** — see [Installation → Docker](installation.md#docker).
- **Tune behaviour** — the full [Configuration](reference/config.md) reference.
