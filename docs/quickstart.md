# Quickstart — your first bridge in a few minutes

This walks you from nothing to a running `claude` bridge you can pick up from
`claude.ai/code`, on a single machine with no authentication (loopback only). For
LAN / remote access, add auth afterwards — see [Networking](networking.md).

## 1. Prerequisites

- **Python 3.11+**.
- The **`claude` CLI on your `PATH`**, authenticated — either logged in via
  `claude`'s interactive login **or** with `ANTHROPIC_API_KEY` set in the
  environment (either satisfies the check). Clauster *spawns* `claude` — it
  doesn't vendor it — and a spawned bridge inherits that authentication. Install
  [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview)
  separately; `clauster doctor` (step 4) confirms it's found and authenticated.
- A **directory that holds your projects** — each child directory becomes a card.

```sh
mkdir -p ~/code/my-first-project    # one child dir = one project card
```

## 2. Install

```sh
uv tool install clauster      # or: pipx install clauster / pip install clauster
```

## 3. Write a minimal config

The only required key is `projects_root`. On loopback, no auth is needed.

```yaml
# clauster.yml
projects_root: ~/code
```

## 4. Check your environment

```sh
clauster doctor
```

`doctor` confirms `claude` is found and new enough, that you're logged in, and
that `projects_root` and the state dir are usable. Fix any ✗ before continuing —
the most common ones are "`claude` not found" (it's not on `PATH`) and
"not logged in" (run a `claude` session first).

## 5. Run it

```sh
clauster run -c clauster.yml
```

Open **<http://127.0.0.1:7621>**. You'll see one card per child directory of
`projects_root` — including `my-first-project`.

## 6. Start your first bridge

On the project card, click **Start**.

- If the directory isn't trusted yet, Clauster shows a **just-in-time trust
  confirm**. Accepting writes the Claude workspace-trust flag for that directory
  *before* the bridge spawns; a trusted directory shows a green shield and starts
  with no prompt. (Trust is Claude's own safety gate — see [Security](security.md).)
- The card moves to **starting**, then **running** once the bridge registers.

!!! note "First-ever spawn"
    Before the first spawn, Clauster marks remote control acknowledged in your
    `~/.claude.json` so the detached bridge isn't stuck on Claude's one-time
    "Enable Remote Control? (y/n)" prompt. This is `claude.auto_enable_remote_control`
    (on by default).

## 7. Pick it up from anywhere

On the running card, use **Open session in Claude** (or scan the **QR code**) to
attach the bridge from `claude.ai/code` or the Claude mobile app — no SSH session
required. Drive the session there; the live debug-log tail and resource metrics
stay visible on the card.

## 8. Stop / resume

- **Stop** ends the bridge.
- **Resume** brings a stopped bridge back. In the default *standard* mode a
  resume starts a fresh context; opt into *true resume* with
  `claude.resume_mode: pty` (POSIX), or recap the prior conversation with
  `claude.resume_recap`. See [the two bridge modes](index.md#the-two-bridge-modes).
- **Start new session** forces a deliberate fresh start on a resumable bridge.

## Next steps

- **Expose it on your LAN / remotely** — a non-loopback bind *requires* enforced
  auth. Generate a hash with `clauster hash-password` and follow
  [Networking](networking.md).
- **Run it as a service** — `clauster install-service {systemd|launchd|windows}`.
- **Run it in Docker** — see [Installation → Docker](installation.md#docker).
- **Tune behaviour** — the full [Configuration](configuration.md) reference.
