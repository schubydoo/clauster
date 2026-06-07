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
with `uvx clauster@<version> …`.

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
