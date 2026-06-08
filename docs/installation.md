# Installation

Clauster requires **Python 3.11+** and the `claude` CLI on the host's `PATH` —
Clauster spawns `claude`, it does **not** vendor it. Install Claude Code
separately and make sure it is new enough (the default floor is
`claude.min_version`, currently `2.1.145`).

## With uv (recommended)

[`uv`](https://docs.astral.sh/uv/) can install Clauster as a standalone tool:

```sh
uv tool install clauster
clauster run -c clauster.yml
```

Upgrade later with:

```sh
uv tool upgrade clauster
```

Or run it **without installing** — `uvx` fetches and runs Clauster in one shot,
handy for a quick try or a one-off command:

```sh
uvx clauster run -c clauster.yml      # or: uvx clauster doctor / hash-password
```

`uvx` re-resolves the package on every invocation, so for a server you keep
running prefer `uv tool install` above; pin a version for a reproducible one-off
with `uvx clauster@<version> run -c clauster.yml`.

## With pip / pipx

```sh
pip install clauster        # or: pipx install clauster
clauster run -c clauster.yml
```

The package installs a single `clauster` console entry point
(`clauster.__main__:main`); `python -m clauster` is equivalent.

## From source (development)

```sh
uv sync --extra dev
cp clauster.yml.example clauster.yml    # edit projects_root
uv run clauster
```

Then open <http://127.0.0.1:7621>. `claude` must be on your `PATH`.

## Docker

A multi-arch (`linux/amd64`, `linux/arm64`) image is published to
`ghcr.io/schubydoo/clauster`. It is built on a `python:3.14-slim-trixie` base,
runs **non-root** with PUID/PGID remapping, and ships a `HEALTHCHECK` against
`/healthz`.

!!! warning "The `claude` CLI is not baked into the image"
    Clauster spawns `claude remote-control`, so you must provide the CLI at
    runtime — mount it onto `PATH` (or build a derived image that installs it) —
    along with the runtime user's `~/.claude` credentials and your projects
    directory.

The image mounts two volumes:

| Mount | Purpose |
| --- | --- |
| `/config` | `clauster.yml` + the `state_dir` |
| `/projects` | the `projects_root` to manage |

The container binds `0.0.0.0`, so it **requires enforced auth to start** (see
[Networking](networking.md)). Generate a password hash without anything on the
host:

```sh
docker compose run --rm clauster clauster hash-password
```

### Docker Compose

A ready-to-edit [`compose.yaml`](https://github.com/schubydoo/clauster/blob/main/compose.yaml)
is included:

```sh
# 1. generate a password hash (runs inside the image)
docker compose run --rm clauster clauster hash-password
# 2. export it single-quoted (single quotes stop the shell expanding the `$`)
export CLAUSTER_AUTH_PASSWORD_HASH='$argon2id$v=19$...'
# 3. point the projects/claude volumes at your host, then start
docker compose up -d
```

The bundled Compose file sets the mandatory auth env vars for a non-loopback
bind:

```yaml
environment:
  CLAUSTER_AUTH_ENABLED: "true"
  CLAUSTER_AUTH_PASSWORD_REQUIRED: "true"
  CLAUSTER_AUTH_PASSWORD_HASH: ${CLAUSTER_AUTH_PASSWORD_HASH:?...}
  PUID: "1000"
  PGID: "1000"
```

!!! note "Hashes in a `.env` file"
    An `$argon2id$…` hash is full of `$`. Exported single-quoted on the command
    line it is safe, but in a `.env` file beside the Compose file you must
    **double every `$` to `$$`** so Compose does not try to interpolate it.

## Running

```sh
clauster run -c clauster.yml
```

`run` is the default subcommand, so bare `clauster` and `clauster -c <cfg>` also
start the server (for backward compatibility). If no config is passed, Clauster
searches `$CLAUSTER_CONFIG`, then `./clauster.yml`, then
`$CLAUSTER_HOME/clauster.yml`.

### Other subcommands

```text
clauster run                  # start the server (default)
clauster hash-password        # generate an argon2id hash for auth.password_hash
clauster doctor               # diagnose config / environment
clauster backup | restore | migrate
clauster install-service {systemd|launchd|windows}
clauster reap-environments    # reap ghost bridge environments (dry-run by default)
clauster usage <transcript>   # token + approximate cost for a session transcript
```

`clauster doctor` confirms `claude` is found and new enough and that
`projects_root` and the state dir are usable — run it before your first spawn
and fix any ✗.

## Run as a systemd service (Linux)

`clauster install-service systemd` **prints** a ready-to-use unit (it does not
install it), so you can review it before writing it into place. Run Clauster as a
**dedicated user** — it spawns `claude` and needs that user's `~/.claude`
credentials:

```sh
# 1. Put the config where the unit expects it (default: /etc/clauster/clauster.yml)
sudo install -Dm600 clauster.yml /etc/clauster/clauster.yml

# 2. Generate the unit with the SAME clauster you installed (so ExecStart points at
#    the right interpreter), run-as the clauster user, and write it into place
clauster install-service systemd --user clauster | sudo tee /etc/systemd/system/clauster.service

# 3. Enable + start; follow the logs with journalctl
sudo systemctl daemon-reload
sudo systemctl enable --now clauster
journalctl -u clauster -f
```

The generated unit:

```ini
[Unit]
Description=Clauster — browser-driven claude remote-control manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=clauster
ExecStart=/path/to/python -m clauster run -c /etc/clauster/clauster.yml
WorkingDirectory=/etc/clauster
Environment=CLAUSTER_CONFIG=/etc/clauster/clauster.yml
Restart=on-failure
RestartSec=5
# Leave detached pty (true-resume) bridges running across a restart (see below).
KillMode=process

[Install]
WantedBy=multi-user.target
```

!!! note "`KillMode=process` keeps bridges alive across a restart"
    Clauster's spawned bridges run inside the service's cgroup, so systemd's
    default `KillMode=control-group` would reap every running bridge — including
    `pty` true-resume sessions — on a `systemctl restart` / `stop`. The generated
    unit sets **`KillMode=process`** so systemd signals only the Clauster process;
    the detached bridges keep running and Clauster reattaches them on startup. A
    deliberate `stop` therefore leaves bridges running (orphaned until the next
    start re-adopts them) — that's intentional, so an upgrade restart doesn't drop
    live coding sessions. Already running an **older unit** without it? Regenerate
    (`clauster install-service systemd …`), reinstall, and `sudo systemctl
    daemon-reload`; `clauster doctor` warns when the loaded `clauster.service`
    still uses a reaping `KillMode`. (A bridge truly lost to a crash or reboot is
    still recoverable with `claude --continue`.)

!!! note "Auth + sandboxing"
    A non-loopback bind **refuses to start without enforced auth** — set the
    `CLAUSTER_AUTH_*` vars and a password hash, or bind to loopback (see
    [Networking](networking.md)). Be conservative with systemd sandboxing:
    Clauster needs the run-as user's real `~/.claude` and spawns `claude`
    subprocesses, so options like `ProtectHome=`, `PrivateUsers=`, or a
    restrictive `SystemCallFilter=` can break it. `NoNewPrivileges=true` plus
    `ProtectSystem=strict` (with the `state_dir` and `~/.claude` listed under
    `ReadWritePaths=`) are reasonable starting points — test a spawn after adding
    any hardening.
