# Installation

Clauster needs the `claude` CLI on the host's `PATH` — Clauster spawns `claude`,
it does **not** vendor it. Install Claude Code separately and make sure it is new
enough (the default floor is the `claude.min_version` config default; run
`clauster doctor` to check the installed `claude` against it). The
`uv` / `pip` / `pipx` installs below also need **Python 3.11+**; the standalone
binary, install script, Scoop, Homebrew, and Nix paths do not.

## Which install method? {#which-install-method}

Ten ways in, one decision: pick the row that matches your host, then jump to
its section — the methods are interchangeable, and every one still needs the
`claude` CLI on `PATH`.

| Method | Platforms | Needs Python 3.11+ | Update with | Pick it when |
| --- | --- | --- | --- | --- |
| [uv](#with-uv-recommended) *(recommended)* | Linux · macOS · Windows | yes | `uv tool upgrade clauster` | You have Python and no strong reason otherwise |
| [pip / pipx](#with-pip-pipx) | Linux · macOS · Windows | yes | `pip install -U clauster` / `pipx upgrade clauster` | You already manage tools with pip/pipx |
| [Install script](#install-script-linux-macos-no-python) | Linux · macOS | no | re-run the one-liner | Fastest checksum-verified binary install, no Python |
| [Install script (PowerShell)](#install-script-windows-powershell) | Windows | no | re-run the one-liner | Same, on Windows |
| [Standalone binary](#standalone-binary-no-python) | Linux (x86_64/arm64) · macOS (arm64/x86_64) · Windows (x86_64) | no | download the next release | You want to verify Sigstore / SLSA provenance yourself |
| [Scoop](#scoop-windows) | Windows | no | `scoop update clauster` | Windows with managed updates |
| [Homebrew](#homebrew-macos-linux) | macOS · Linux | no | `brew update && brew upgrade clauster` | Your host is brew-managed |
| [Nix](#nix-flake) | Linux · macOS | no | `nix profile upgrade` | Your host is Nix/flake-managed |
| [From source](#from-source-development) | anywhere with `uv` | yes | `git pull && uv sync --extra dev` | Contributing to Clauster |
| [Docker](#docker) | any `linux/amd64` / `linux/arm64` host | no | pull the new image tag | Container stacks — needs `claude` mounted in and enforced auth |

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

## With pip / pipx {#with-pip-pipx}

```sh
pip install clauster        # or: pipx install clauster
clauster run -c clauster.yml
```

The package installs a single `clauster` console entry point
(`clauster.__main__:main`); `python -m clauster` is equivalent.

## Install script (Linux & macOS, no Python) {#install-script-linux-macos-no-python}

The quickest way to get the standalone binary. The script detects your OS +
architecture, downloads the matching binary from the latest release, **verifies
its SHA-256** against the release's signed `SHA256SUMS`, and installs it to
`~/.local/bin`:

```sh
curl -fsSL https://raw.githubusercontent.com/schubydoo/clauster/main/install.sh | bash
```

`wget -qO- <url> | bash` works too. Environment overrides:

| Variable | Effect |
| --- | --- |
| `CLAUSTER_VERSION` | Pin a version (e.g. `0.10.0`) instead of the latest release |
| `CLAUSTER_INSTALL_DIR` | Install directory (default `~/.local/bin`) |

**Prefer to read before you pipe?** Piping a script straight into `bash` runs
remote code sight-unseen. To review it first, download, inspect, then run:

```sh
curl -fsSL -o clauster-install.sh https://raw.githubusercontent.com/schubydoo/clauster/main/install.sh
less clauster-install.sh && bash clauster-install.sh
```

If no binary is published for your platform yet, the script prints a `pip`/`uvx`
fallback and exits. On **Windows**, use the [PowerShell
installer](#install-script-windows-powershell) or [Scoop](#scoop-windows).

The script authenticates the binary by SHA-256 against the release's
`SHA256SUMS`, which it trusts over HTTPS from GitHub — the standard
`curl … | bash` trust model. For the stronger keyless-signature check (verifying
`SHA256SUMS` and the binary against their `.sigstore.json` bundles), download the
binary yourself and use the `cosign` / `gh attestation verify` flow under
[Standalone binary](#standalone-binary-no-python) below.

## Install script (Windows, PowerShell)

The PowerShell equivalent of the script above. It resolves the latest release,
downloads `clauster.exe`, **verifies its SHA-256** against the release's
`SHA256SUMS`, installs it under `%LOCALAPPDATA%\Programs\clauster`, and adds that
directory to your user `PATH`:

```powershell
irm https://raw.githubusercontent.com/schubydoo/clauster/main/install.ps1 | iex
```

Environment overrides: `$env:CLAUSTER_VERSION` pins a version, and
`$env:CLAUSTER_INSTALL_DIR` overrides the install directory. To review the script
before running it (it trusts `SHA256SUMS` over HTTPS, the same as the Unix
one-liner):

```powershell
irm https://raw.githubusercontent.com/schubydoo/clauster/main/install.ps1 -OutFile install.ps1
notepad install.ps1 ; .\install.ps1
```

The binary is Sigstore-signed but not authenticode-signed, so SmartScreen may
warn on first run — the installer clears the file's mark-of-the-web after the
checksum passes, but a fresh download via the browser may still prompt.

## Standalone binary (no Python)

Prefer to grab the file yourself? Each release attaches a single-file binary per
OS + architecture, built with PyInstaller. It still spawns `claude`, so the CLI
must be on your `PATH`.

| OS / arch | Asset |
| --- | --- |
| Linux x86_64 | `clauster-<version>-linux-x86_64` |
| Linux arm64 | `clauster-<version>-linux-arm64` |
| macOS arm64 (Apple Silicon) | `clauster-<version>-macos-arm64` |
| macOS x86_64 (Intel) | `clauster-<version>-macos-x86_64` |
| Windows x86_64 | `clauster-<version>-windows-x86_64.exe` |

Download from the [latest release](https://github.com/schubydoo/clauster/releases/latest),
**verify the checksum** against the release's `SHA256SUMS`, then run it. The asset
name carries the version, so resolve it first (GitHub's `latest/download/`
redirect needs the exact filename):

```sh
# Linux x86_64 — swap clauster-$VER-linux-x86_64 for clauster-$VER-macos-arm64 on Apple Silicon
VER=$(curl -fsSL https://api.github.com/repos/schubydoo/clauster/releases/latest \
  | sed -n 's/.*"tag_name": *"v\([^"]*\)".*/\1/p')
curl -LO https://github.com/schubydoo/clauster/releases/download/v$VER/clauster-$VER-linux-x86_64
curl -LO https://github.com/schubydoo/clauster/releases/download/v$VER/SHA256SUMS
sha256sum --check --ignore-missing SHA256SUMS    # expect: clauster-$VER-linux-x86_64: OK
chmod +x clauster-$VER-linux-x86_64
./clauster-$VER-linux-x86_64 run -c clauster.yml
```

Every binary is also **Sigstore-signed** (keyless) — a `<asset>.sigstore.json`
bundle sits beside it, and the release carries SLSA provenance
(`*.intoto.jsonl`). Verify the build with the
[`cosign`](https://docs.sigstore.dev/) / `gh attestation verify` toolchain if you
want the full supply-chain check.

!!! note "Unsigned for the OS, on purpose"
    The binaries are Sigstore-signed but not yet OS code-signed, so the first run
    needs one extra step. On **macOS**, Gatekeeper quarantines a downloaded
    binary — clear it with `xattr -d com.apple.quarantine
    clauster-<version>-macos-arm64`, or right-click → **Open** once. On
    **Windows**, SmartScreen may warn on first launch; choose **More info → Run
    anyway** (installing via Scoop, below, avoids the browser-download prompt).

!!! note "The live terminal view needs the `pty` extra"
    The optional read-only live terminal view ([`pty_screen_enabled`](reference/config.md))
    depends on [`pyte`](https://pypi.org/project/pyte/), which
    is LGPL-licensed and so **is not bundled in the standalone binary**. On the binary, install
    it with `clauster deps install pty` (the binary bundles `pip` and side-installs the wheel
    into `<state_dir>/deps`, which it adds to `sys.path` at startup — no LGPL code is relinked
    into the Apache-2.0 binary); restart afterwards. From a `pip`/`uv`/`pipx` install, use the
    `[pty]` extra instead (`pip install 'clauster[pty]'`). As a manual alternative on the binary,
    `pip install --target=/your/chosen/dir pyte` and set `CLAUSTER_PYTE_PATH` to that directory.

!!! note "Optional extras"
    A few capabilities live behind optional `pip` **extras** that the default install and
    the signed standalone binary deliberately do not bundle. The `pty` extra
    (`pip install 'clauster[pty]'`) enables the read-only live terminal view (`pyte`, LGPL)
    and the Windows ConPTY keeper for Interactive Session (`pywinpty`, Windows-only); the
    `notify` extra (`pip install 'clauster[notify]'`) enables outbound bridge-lifecycle
    notifications (`apprise`). Each is optional — a missing extra only leaves its feature
    dormant, never breaks a launch. `clauster doctor` reports every extra (`OK` when present,
    `WARN` when missing, with the install hint), and on the standalone binary the hint points
    at the side-install path instead of `pip`.
    On the standalone binary you don't need to set `CLAUSTER_PYTE_PATH` per-extra: the binary
    also adds a managed `<state_dir>/deps` directory to its import path at startup, so anything
    installed there (`pip install --target=<state_dir>/deps <dist>` from any Python) loads on the
    next start —
    [`clauster deps`](reference/cli.md#clauster-deps-inspect-and-manage-optional-extras)
    manages that directory (`clauster deps list` shows each extra's status; `clauster deps install
    <extra>` / `uninstall <extra>` populate or clear it).

## Scoop (Windows)

On Windows, [Scoop](https://scoop.sh) installs the standalone binary and keeps it
updated. This repository doubles as a Scoop bucket:

```powershell
scoop bucket add clauster https://github.com/schubydoo/clauster
scoop install clauster
clauster run -c clauster.yml
```

`scoop update clauster` picks up new releases automatically (the manifest tracks
GitHub releases and re-verifies the checksum on each update).

## Homebrew (macOS & Linux) {#homebrew-macos-linux}

[Homebrew](https://brew.sh) installs the standalone binary on macOS (Apple Silicon
and Intel) and Linux (x86_64 and arm64) from the project's tap:

```sh
brew install schubydoo/clauster/clauster
clauster run -c clauster.yml
```

That auto-taps `schubydoo/homebrew-clauster`; the two-step form is `brew tap
schubydoo/clauster` then `brew install clauster`. Upgrade with `brew update && brew
upgrade clauster` — the tap mirrors each release automatically and Homebrew verifies
the SHA-256, so the unsigned-binary prompt the manual download warns about doesn't
apply here. `clauster` still spawns `claude`, so keep Claude Code on your `PATH`.

## Nix (flake)

The repository is a [Nix flake](https://nixos.org/) exposing the standalone binary
for `x86_64`/`aarch64` Linux and macOS. Run it ad hoc, or install it into a profile:

```sh
# Run without installing (forward args after --):
nix run github:schubydoo/clauster -- run -c clauster.yml

# Or install persistently:
nix profile install github:schubydoo/clauster
```

The flake pins each release's signed binary by SHA-256, so `nix profile upgrade`
picks up new releases. Windows is not a Nix target — use Scoop there.

## From source (development)

```sh
uv sync --extra dev
cp clauster.yml.example clauster.yml    # edit projects_root
uv run clauster
```

Then open <http://127.0.0.1:7621>. `claude` must be on your `PATH`.

## Docker

A multi-arch (`linux/amd64`, `linux/arm64`) image is published to
`ghcr.io/schubydoo/clauster`. It is built on an `alpine` (musl) base with
explicitly pinned apk packages, runs **non-root** with PUID/PGID remapping, and
ships a `HEALTHCHECK` against `/healthz`.

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

Everything else — the secret/token helpers, `doctor`, backup/restore,
`config reconcile`, `keepers`, `deps`, the headless read/write commands
(`projects`, `status`, `sessions`, `logs`, `open`, `start`, `stop`), `usage`,
and the MCP server — is catalogued in
[CLI commands](reference/cli.md).

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

# 2. Generate the unit with the SAME clauster you installed (so ExecStart resolves to
#    the right clauster entry point — the binary/console-script for a uv/pip/pipx/binary
#    install, or `python -m clauster` only for a bare dev interpreter), run-as the
#    clauster user, and write it into place
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
ExecStart=/home/clauster/.local/bin/clauster run -c /etc/clauster/clauster.yml
WorkingDirectory=/etc/clauster
Environment=CLAUSTER_CONFIG=/etc/clauster/clauster.yml
# Spawned bridges inherit this PATH (see note below); ~/.local/bin covers uv-installed tools.
Environment="PATH=/home/clauster/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
Restart=on-failure
RestartSec=5
# Leave detached pty (true-resume) bridges running across a restart (see below).
KillMode=process

[Install]
WantedBy=multi-user.target
```

`install-service` auto-detects how Clauster is installed and bakes the entry point
straight into `ExecStart`: a frozen standalone binary or a `clauster` console
script (uv tool / pip / pipx) is invoked directly, as shown above. Only a bare dev
interpreter falls back to the `python -m clauster run …` form.

!!! note "`KillMode=process` keeps bridges alive across a restart"
    The generated unit sets **`KillMode=process`** (not systemd's default
    `KillMode=control-group`), so a `systemctl restart` / `stop` signals only the
    Clauster process — detached bridges, including `pty` true-resume sessions, keep
    running and Clauster reattaches them on startup. `clauster doctor` warns if an
    older loaded unit still uses a reaping `KillMode`. See
    [Operations → the `KillMode` / `systemctl restart` caveat](operations.md#restart)
    for the full rationale and recovery steps.

!!! note "Bridges inherit the unit's `PATH`"
    Under systemd a service gets a minimal default `PATH`, and Clauster propagates
    its environment to every `claude` bridge it spawns — so without a `PATH` in the
    unit, an agent inside a bridge can't resolve user-local tools (`uv`, `ruff`,
    `pytest`, …) that work fine in your interactive shell. The generated unit bakes
    the run-as user's `~/.local/bin` plus the standard system dirs (resolved from
    the `--user` you pass, so regenerate if you change it). For **shell-managed**
    toolchains a static directory can't cover — nvm/pyenv `node`, `cargo`, Go —
    extend it with [`claude.path_append` / `claude.env`](reference/config.md#claude-binary-bridge-spawn-claudeconfig)
    in `clauster.yml`; those append to (never replace) this baked `PATH`.

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

## Uninstalling

The uninstaller is the counterpart to the install script. It auto-detects how
Clauster was installed (the standalone binary, or a `uv tool` / `pipx` / `pip` /
`scoop` package), removes the right artifact, stops and removes a Clauster service
unit if one was installed, and removes the state directory (`clauster.db`,
`state.json`, `hosted_state.json`, `tls/`, `backups/`, sockets, logs) and the
config yaml.

**Linux & macOS:**

```sh
# Preview first — lists exactly what would be removed, changes nothing:
curl -fsSL https://raw.githubusercontent.com/schubydoo/clauster/main/uninstall.sh | bash -s -- --dry-run

# Then remove:
curl -fsSL https://raw.githubusercontent.com/schubydoo/clauster/main/uninstall.sh | bash
```

**Windows (PowerShell):**

```powershell
irm https://raw.githubusercontent.com/schubydoo/clauster/main/uninstall.ps1 | iex
# Preview first:  &([scriptblock]::Create((irm https://raw.githubusercontent.com/schubydoo/clauster/main/uninstall.ps1))) -DryRun
```

Options (same names on both, `--flag` on the shell script, `-Flag` on PowerShell):

- `--dry-run` / `-DryRun` — show what would be removed without removing anything.
- `--keep-config` / `-KeepConfig` — preserve `clauster.yml` for a future reinstall
  (moved aside to a printed backup path).
- `--keep-data` / `-KeepData` — preserve `clauster.db` the same way.
- `--keep-deps` / `-KeepDeps` — preserve side-installed optional deps (the `pty`/`notify`
  extras and Shawl in `<state_dir>/deps`) the same way, so a reinstall needn't re-download
  them. The uninstaller enumerates what it found there before removing anything.
- `-y` / `-Yes` — skip the confirmation prompt.

The uninstaller is deliberately conservative: it never deletes a path outside the
known Clauster locations, refuses a `state_dir` that resolves to your home directory
or a drive root, and — if it can't identify how Clauster was installed — reports what
it found and exits without deleting anything rather than guessing. It leaves Claude
Code (the `claude` CLI) untouched, since that is installed separately.

!!! note "systemd service needs privileges"
    If you installed the systemd unit, removing it needs `sudo`; the script prints the
    exact `systemctl stop/disable` + `rm` commands it runs so you can see (or run) the
    privileged step yourself. A relocated `state_dir` or config is picked up from the
    same `CLAUSTER_STATE_DIR` / `CLAUSTER_CONFIG` overrides the app uses.
