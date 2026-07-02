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

## 0.12 → 0.13: SQLite-only; `database_url` removed

0.13 commits to SQLite as the only persistence substrate (#796) — the
never-really-supported `database_url` config key (a Postgres DSN escape hatch)
is gone. Config schema is additive-only, so a leftover `database_url` line in
an older `clauster.yml` is **silently ignored on load**, not rejected — no
action required. `clauster.db` under `state_dir` remains the only database;
nothing about the schema, migrations, or `state_dir` layout changes.

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
