# Upgrading Clauster

Clauster has **no in-app self-update** — by design. A web service that rewrites
its own code and restarts itself is a security and operability footgun, so
upgrades are an external operation: update the package, then restart. Run
`clauster doctor` after upgrading to confirm config + environment are still sane
(it also warns when your checkout is behind its upstream).

Always **back up first** — it's a few seconds and makes any upgrade reversible:

```bash
clauster backup ./clauster-backups/      # tar.gz of state_dir + config
```

The on-disk **database** schema is migrated **automatically and fail-closed** on
startup — clauster refuses to serve until the migration succeeds, so you never
run a database migration by hand. (The separate `clauster migrate` command is a
*legacy* helper that only upgrades an older flat-file `state.json`; on a 0.12+
deployment it has nothing to do.)

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

`clauster doctor` will tell you whether `uv sync` is needed and whether the
checkout is behind `origin`. (The database migrates automatically on startup;
`clauster migrate` is only for folding in a pre-0.12 flat-file `state.json`.)

> **Standard** bridges are detached and survive a Clauster restart, so the
> upgrade only refreshes the manager and doesn't interrupt them.
>
> **pty (true-resume) bridges do *not* survive a `systemctl restart`** — the
> default `KillMode=control-group` reaps the whole service cgroup, killing the
> bridge. Its transcript is preserved (resume with `claude --continue`), but the
> live session ends. `clauster doctor` flags a unit with the reaping default;
> `sudo clauster install-service systemd --write` then installs a
> `KillMode=process` unit so pty bridges survive *future* restarts (the one
> restart that applies the new unit still reaps the current bridges).
