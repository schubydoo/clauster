# Upgrading Clauster

Clauster has **no in-app self-update** — by design. A web service that rewrites
its own code and restarts itself is a security and operability footgun, so
upgrades are an external operation: update the package, then restart. Run
`clauster doctor` after upgrading to confirm config + environment are still sane
(it also warns when your checkout is behind its upstream).

> **Not the same as the in-app "Restart Clauster" action.** The config editor's
> **Restart Clauster** button (#483) re-execs the *currently installed* process in
> place (`os.execv` — same code, same PID) only to reload a **config** change. It
> never fetches, installs, or rewrites code, so the no-self-**update** stance above
> is unchanged: a version upgrade is still update-the-package-then-restart. The
> in-app restart simply spares you a shell for a config reload; the live-session
> caveat below applies to it exactly as to any restart (under a default
> `KillMode=control-group` unit a restart reaps the cgroup and takes live bridges
> with it; a `KillMode=process` unit — what `clauster install-service` writes — does
> not, so they survive).

Always **back up first** — it's a few seconds and makes any upgrade reversible:

```bash
clauster backup -c /path/to/clauster.yml -o ./clauster-backups/   # tar.gz of state_dir + config
```

The on-disk **database** schema is migrated **automatically and fail-closed** on
startup — clauster refuses to serve until the migration succeeds, so you never
run a database migration by hand. (The separate `clauster migrate` command is a
*legacy* helper that only upgrades an older flat-file `state.json`; on a 0.12+
deployment it has nothing to do.)

## 0.12 → 1.0: a bridge's `id` in `clauster mcp` is now its instance id

`list_sessions` reported each bridge's **project name** as its `id`. A project can
run several bridges at once — one standard (Server Mode) bridge plus any number of
Interactive Sessions — so that id was not unique: every bridge on a project came
back with the same one. As of 1.0 a bridge's `id` is its stable **instance id**, and
the project name stays available as the separate `project` field.

This matters if you consume `list_sessions` output. A working session's
`parent_instance` now joins to exactly one bridge `id`, which it could not do
before. If you keyed bridges by `id` and assumed it was the project name, read
`project` instead.

`session_status` still accepts a **project name** for a bridge when that project
runs exactly one. When it runs several, the name no longer identifies a single
session, so instead of returning whichever bridge was registered first you get:

```json
{"found": false, "id": "myproject", "ambiguous": ["<bridge id>", "<bridge id>"]}
```

Retry with one of the returned ids. `stop_session` / `resume_session` accept either
form, so they keep working — but note a project name that matches several bridges
still resolves to the one the dashboard displays rather than reporting the
ambiguity, so pass an **instance id** when you mean a specific bridge.

## 0.12 → 1.0: `clauster mcp` write tools default off

The [`clauster mcp`](docs/mcp.md) stdio server's **write** tools —
`spawn_session`, `stop_session`, `resume_session` — shipped always-on in #950. As
of 1.0 they are gated behind a new **`mcp.allow_writes`** config key that
**defaults off**, so the stdio MCP surface is **read-only by default**
(`list_sessions` / `session_status` only). The stdio transport is local-privileged
and unauthenticated, so understating its capability — a banner/help that said
"read-only" while the surface could start and stop bridges — was the security-
relevant misstatement this closes (#1010).

If an MCP client relied on driving the bridge lifecycle through `clauster mcp`,
re-enable it explicitly:

```yaml
mcp:
  allow_writes: true
```

Like the `config_write` / `login_shepherd` gates, `mcp.allow_writes` is
file/CLI-managed only (**not** web-editable). The read tools are unchanged.

## 0.12 → 1.0: the dashboard now blocks cross-site requests even with no password

**What changed, in plain terms.** A website you open in your browser can quietly send
requests to programs running on your own computer — clauster included. Before 1.0, when
clauster had no password set (the default), it didn't check *where* a request came from.
So a malicious page you happened to visit could drive your dashboard (start/stop sessions,
trust a folder, restart the service) or read your live session output, without you ever
seeing it. As of 1.0 clauster checks the origin of every state-changing request and every
live-view (WebSocket) connection and rejects any that came from somewhere else. (Requests
with no origin at all — command-line tools, scripts, `curl` — are unaffected: only a
browser attaches an origin, so there is nothing to spoof.)

**Do you need to do anything?** Check your config's `host:` setting first — that, not the
address you browse, is what decides:

- **`host:` is `127.0.0.1` or `localhost` (the default), and you browse it at that same
  address and port.** Nothing to do. You're simply safer.
- **`host:` is loopback, but you reach it some *other* way** — you need to list that
  address (one line, below):
  - a **tunnel or reverse proxy** in front of clauster (Cloudflare Tunnel, Tailscale
    Serve, nginx/Caddy) — the address is the public hostname, not `localhost`;
  - an **SSH port-forward onto a different local port**, e.g.
    `ssh -L 9000:localhost:7621` → you browse `http://localhost:9000`, and the *port*
    no longer matches. (Forwarding to the *same* port keeps working.)
- **`host:` is anything else — `0.0.0.0`, a LAN IP, or Docker (the image binds `0.0.0.0`).**
  Then clauster trusts **nothing** automatically, and you must list the address you browse
  to **even if that address is `http://localhost:7621`**. This is the case most likely to
  surprise you: the container is on `0.0.0.0` internally, so browsing it at `localhost`
  still needs the entry.

In the second and third cases your own buttons and live views are refused until you
list the address you actually browse to:

```yaml
auth:
  allowed_origins: ["http://localhost:9000"]   # the address you type into the browser
```

Use the address **as it appears in your browser's URL bar** — scheme, host, and port — not
clauster's internal bind address. It fails loudly, never silently: a refused action, or a
live view that won't connect, with `origin check failed` in the response.

If clauster **already has a password**, nothing changes for you here — a
non-loopback or proxied deployment has always needed `auth.allowed_origins`. This change
only extends that same requirement to deployments running *without* a password, which
previously skipped the check entirely. (And if you're reachable beyond your own machine,
setting a password is worth doing regardless.)

## 0.12 → 1.0: SQLite-only; `database_url` removed

1.0 commits to SQLite as the only persistence substrate (#796) — the
never-really-supported `database_url` config key (a Postgres DSN escape hatch)
is gone. A leftover `database_url` (the YAML key **or** the
`CLAUSTER_DATABASE_URL[_FILE]` env override) is **ignored with a logged
warning**, not rejected — your data goes to SQLite regardless. Remove the
key/env to silence the warning. `clauster.db` under `state_dir` remains the only database;
nothing about the schema, migrations, or `state_dir` layout changes.

## 0.12 → 1.0: Docker image is now Alpine-based

The published container image moved from Debian-slim to an **Alpine (musl)** base — it's
smaller and installs explicitly pinned, Renovate-tracked packages instead of an unpinned
`apt upgrade`. Running the stock image (`docker run …`) is unchanged. Two setups **are**
affected:

- **You build a derived image `FROM` the clauster image**: use `apk` (not `apt`) to add
  packages, and note that **`su-exec` replaces `gosu`** as the privilege-drop helper.
- **You mount the host `claude` binary into the container** (the README /
  `compose.yaml` pattern): a glibc build from your host **no longer execs** on the musl
  base — launches fail with `exec /usr/local/bin/claude: no such file or directory` (or
  `claude: not found` via a shell) even though the file is present; the kernel is
  reporting the missing glibc loader, not the binary. Mount a **musl** build instead, or
  build a derived image that installs one:
  `apk add --no-cache bash curl && curl -fsSL https://claude.ai/install.sh | bash`
  (the installer needs `bash`; busybox `sh` can't run it).

## 0.11 → 0.12: the state store moves to SQLite

0.12 is the first release with a **database**. State now lives in `clauster.db`
(SQLite) under your `state_dir`, replacing the flat `state.json` /
`hosted_state.json`. The upgrade is automatic — there is **no manual step**:

- On the **first 0.12 start**, clauster creates `clauster.db`, runs the schema
  migration **fail-closed** (it won't serve if the migration fails), then
  **imports your existing `state.json` and `hosted_state.json` once** and renames
  them to `*.imported`. Later starts are idempotent — no re-import.
- **Do not run `clauster migrate` for this** — it only touches the legacy flat
  files, not the database. Just upgrade the package and restart.
- **Back up first** (`clauster backup`): the database is now the live store, so a
  pre-upgrade snapshot is your rollback.

**If the migration fails, or you need to roll back to 0.11:** the migration is
fail-closed, so a failure leaves the service *down*, not half-migrated. The
simplest recovery is to restore the pre-upgrade `clauster backup` tarball and
reinstall the prior version. To revert by hand instead: stop clauster, delete
`clauster.db`, rename `state.json.imported` / `hosted_state.json.imported` back
(drop the `.imported` suffix), and reinstall the prior clauster version.

## Upgrade, by install method

Match how you installed (the same rows as the
[installation decision table](https://schubydoo.github.io/clauster/installation/#which-install-method)),
then restart however you run it — systemd unit, supervisor, container, terminal:

| Installed via | Upgrade with |
| --- | --- |
| uv | `uv tool upgrade clauster` |
| pip / pipx | `pip install -U clauster` / `pipx upgrade clauster` |
| Install script (Linux & macOS) | re-run the one-liner — it fetches and checksum-verifies the latest release |
| Install script (Windows, PowerShell) | re-run the one-liner |
| Standalone binary | download the next release, verify it against `SHA256SUMS`, replace the file |
| Scoop | `scoop update clauster` |
| Homebrew | `brew update && brew upgrade clauster` |
| Nix | `nix profile upgrade` |
| Docker / GHCR | pull the new tag + recreate — see below |
| From source | `git pull` + restart — see below |

## PyPI install

```bash
pip install -U clauster      # or: uv pip install -U clauster
# then restart however you run it (systemd unit, supervisor, terminal, …)
```

## Docker / GHCR

```bash
docker pull ghcr.io/schubydoo/clauster:latest   # or a pinned :vX.Y.Z tag
docker compose up -d                              # recreate the container
```

Your `state_dir` and config must be on a mounted volume so they survive the
container swap (see the README's Docker section and `clauster.yml.example`).

## From source (editable git + venv — local development)

If you run clauster from a git checkout (an **editable** install, typically for
local development), a `git pull` + restart is all it takes — no reinstall unless
dependencies changed. (Production/dogfood deploys should follow the PyPI install
path above, not an editable checkout.)

```bash
cd /path/to/clauster
git fetch && git checkout main && git pull          # pull the new code
uv sync --extra dev   # ONLY if pyproject/uv.lock changed (deps); otherwise skip
# clauster migrate -c /path/to/clauster.yml   # SKIP on 0.12+: the DB migrates automatically.
#                                              # Run ONLY to fold in a pre-0.12 flat-file state.json.
sudo systemctl restart clauster                     # picks up the new code
```

`clauster doctor` will tell you whether the checkout is behind `origin` (its
`version` check compares HEAD against the last-fetched upstream). It does **not**
detect dependency drift, so re-run `uv sync --extra dev` whenever
`pyproject.toml` / `uv.lock` changed (per the code block above). (The database
migrates automatically on startup; `clauster migrate` is only for folding in a
pre-0.12 flat-file `state.json`.)

## Rolling back

Any upgrade is reversible if you took the pre-upgrade backup:

1. Stop Clauster.
2. Restore the tarball: `clauster restore <archive> --state-dir ~/.clauster`
   (point `--state-dir` at your configured `state_dir`; add
   `--config-out /path/to/clauster.yml` to also write the config back, and
   `--force` to overwrite a non-empty target).
3. Reinstall the **prior** version (e.g. `uv tool install clauster==<old>`,
   `pip install clauster==<old>`, pin `CLAUSTER_VERSION=<old>` when re-running
   the install script, or run the older binary / image tag) and start it.

Skipped the backup? If the upgrade ran a schema migration, Clauster snapshotted
`clauster.db` first: the five most recent pre-migration copies live under
`state_dir/backups/` as `pre-<rev-before>-<rev-after>-<timestamp>.db`
(`db.backup_before_migrate`, on by default). Stop Clauster, copy the snapshot
back over `state_dir/clauster.db`, **delete any stale `clauster.db-wal` /
`clauster.db-shm` sidecars** (the snapshot is a self-contained `VACUUM INTO`
copy; a leftover WAL belongs to the migrated database and must not be
replayed), and start the matching **older** binary — a newer one would
immediately re-run the same migration. This recovers the database only, not the rest of
`state_dir` or your config.

> **Server Mode** bridges are detached and survive a Clauster restart, so the
> upgrade only refreshes the manager and doesn't interrupt them.
>
> **Interactive Session bridges also survive a `systemctl restart`** — *as long as
> the unit uses `KillMode=process`, which is what `clauster install-service` writes
> by default*, so a clauster-managed install is already safe (the surviving keeper
> is reattached on startup). They're only reaped under systemd's **default**
> `KillMode=control-group`, which kills the whole service cgroup (`clauster doctor`
> flags that case; `sudo clauster install-service systemd --write` installs the
> `KillMode=process` unit). One caveat during a unit migration: the single restart
> that *applies* a new `KillMode=process` unit still runs under the old unit, so it
> reaps whatever bridges are live at that moment (transcript preserved; resume with
> `claude --continue`).
