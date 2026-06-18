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

State is forward-compatible, but if a release changes the on-disk schema, run
`clauster migrate` once after upgrading (it `.bak`s the old state first).

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
clauster migrate -c /path/to/clauster.yml           # ONLY if the schema changed
sudo systemctl restart clauster                     # picks up the new code
```

`clauster doctor` will tell you whether `uv sync` / `migrate` are actually
needed and whether the checkout is behind `origin`.

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
