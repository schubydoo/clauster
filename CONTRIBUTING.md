# Contributing

Thanks for your interest in Clauster!

## Code of conduct

This project adheres to the [Contributor Covenant](https://github.com/schubydoo/clauster/blob/main/CODE_OF_CONDUCT.md) code of
conduct. By participating, you are expected to uphold it; please report
unacceptable behavior as described there.

## Dev setup

```sh
uv sync --extra dev
npm ci                                 # markdownlint-cli2 (pinned, integrity-checked)
uv run pre-commit install              # auto-lint on commit (ruff + yaml/markdown)
cp clauster.yml.example clauster.yml   # edit projects_root
uv run clauster run -c clauster.yml
```

A [`justfile`](https://github.com/schubydoo/clauster/blob/main/justfile) and a [`Makefile`](https://github.com/schubydoo/clauster/blob/main/Makefile) wrap these commands, so
`just setup` / `make setup` do the first three lines and `just check` /
`make check` run every pre-PR gate below in one shot. Both run the same commands —
use whichever you have (`just` is a separate install; `make` ships with most
systems). The `uv run …` invocations documented here remain the source of truth.
Local dev pins Python **3.13** via [`.python-version`](https://github.com/schubydoo/clauster/blob/main/.python-version) to match
the CI coverage-gate interpreter (`uv` still supports the full 3.11+ floor).

## Before opening a PR

- **Tests + coverage** — `uv run pytest`; the suite must pass and stay at or
  above the **96%** coverage gate (enforced in CI on all three OSes; Windows
  measures through `.coveragerc-win`, which excludes the POSIX/ConPTY code it
  can't run so it holds the same floor).
- **Browser E2E (opt-in)** — `scripts/e2e.sh` runs the real-Chromium suite in
  `tests/e2e` (excluded from the default `pytest` run and from the required CI
  gate). It needs Node/npm: the script `npm ci`-installs the version-pinned
  [`agent-browser`](https://github.com/vercel-labs/agent-browser) CLI from
  `tests/e2e/package-lock.json` and downloads a matched headless Chromium on
  first run. Run it when you touch the dashboard UI or `tests/e2e/**`; the
  manual-flow checklist lives in `tests/E2E_CHECKLIST.md`.
- **Lint, format, types** —
  `uv run ruff check . && uv run ruff format --check . && uv run pyright src/clauster`
- **Docs lint (Markdown + YAML)** — `bash scripts/lint-docs.sh` (markdownlint-cli2
  from `npm ci`, pinned in `package.json`; `yamllint` from `uv sync --extra dev`).
  Same command CI runs in the `lint` job. The script calls `npx --no-install`, which
  needs a local `node_modules`: in a fresh git worktree, run `npm ci` there first.
  Without it the lookup walks up to a parent checkout's `node_modules` and uses that
  version, or fails when the worktree sits outside any such checkout.
- **pre-commit (recommended)** — `uv run pre-commit install` once; ruff
  (check + format), yamllint, and markdownlint then run automatically on each
  commit using the same pinned tools as CI. Check everything on demand with
  `uv run pre-commit run --all-files`. It runs only the hooks for changed file
  types; CI remains the full-repo backstop.
- **Security checks** — nothing extra to run locally: Bandit-style SAST is ruff's
  `S` rules, already covered by the `ruff check .` above. CI additionally runs
  CodeQL, Trivy (filesystem + image), dependency review, and a workflow audit
  (zizmor).
- **Conventional Commits** — your PR **title** must follow
  [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `docs:`, `chore:`, `ci:`, …). PRs are squash-merged, so the
  title becomes the commit subject; CI enforces this. (Versioning and the changelog
  are driven by changesets — see the next bullet — not the commit type.)
- **Changeset** — any **user-facing** change needs a `.changeset/*.md` fragment.
  clauster is changesets-only: fragments drive both the version bump and the
  changelog, so a change with no fragment ships with no release note **and** no
  version bump. Create one with `knope document-change`, or add a file by hand:

  ```markdown
  ---
  default: patch
  ---

  A one-line summary of the change (becomes a changelog bullet).
  ```

  Use `default: minor` for a feature, `patch` for a fix, `major` for a breaking
  change, or `perf` / `security` / `build` for those sections (each a patch bump).
  The key really is the literal word `default` — knope silently ignores a fragment
  keyed to anything else, so it would ship with no changelog entry and no version
  bump; `scripts/lint_changesets.py` (pre-commit and CI) rejects that.
  The file name matters just as much: knope reads `.changeset/*.md` and nothing else,
  so `a.txt`, `a.MD` or `sub/a.md` is dropped just as silently — the linter rejects any
  entry in `.changeset/` that is not a top-level fragment whose extension is exactly
  `.md` (only `.gitkeep`, OS droppings like `.DS_Store` / `Thumbs.db`, and editor
  scratch files such as `.a.md.swp` are exempt).
  You don't need to add a PR link — knope appends `([#NNN])` for the PR that
  introduces the changeset, at release time.
  Keep the summary to a single concise line that gets straight to the point;
  it renders as one changelog bullet, and a multi-line body breaks the heading format.
  Internal-only PRs (CI, refactor, tests, docs) need no changeset — add the
  `no-changelog` label to silence the advisory `Changeset check`. **Don't hand-edit
  `CHANGELOG.md` or bump the version in `pyproject.toml` / `src/clauster/__init__.py`**
  — knope owns those, regenerating them in the release PR.

  Any same-repository PR that touches `src/` without a changeset trips the advisory
  `Changeset check`, which nags until you add a `.changeset/*.md` fragment by hand
  (or apply the `no-changelog` label for an internal-only change). Fork contributors
  must add the fragment manually — this check does not run on fork PRs. Confirm the
  bump type (`patch` / `minor` / `major` / `perf` / `security` / `build`) and the
  summary sentence match what your change actually does before you commit it.

CI runs the test suite on **Linux, macOS, and Windows** across Python 3.11–3.14
(all merge-blocking; each OS enforces a consistent 96% coverage gate — Windows
via `.coveragerc-win`, which excludes the POSIX/ConPTY code it can't run), plus
lint, security scanners, and dependency review. Internal PRs run a representative
subset of the OS/Python legs; the full matrix runs on release and fork PRs.

## Branching and review

- **Branch off fresh `origin/main`** (`git fetch` first) and open a PR — every
  change merges through one; the branch ruleset rejects direct pushes to `main`.
  PRs always target `main`; don't stack one PR on another's branch.
- **History is linear and PRs are squash-merged.** If your branch falls behind,
  `git rebase origin/main` — the instinctive `git merge main` produces a merge
  commit the ruleset will reject. The ruleset is also strict-up-to-date, so a
  stale base shows the PR as BEHIND until rebased.
- **[Greptile](https://www.greptile.com/) reviews every PR automatically** and
  usually comments within a few minutes of a push. Read its summary (a green
  score does not mean zero findings), then reply **and** resolve each inline
  thread on the thread itself — unresolved review threads block the merge.
- **The maintainer may add a second Claude review** to a PR, usually when Greptile
  is rate-limited or a change warrants another pass. You may see its comments on
  your PR. Its *verdict* is advisory — it comments, it never requests changes — but
  its **inline threads block the merge exactly like Greptile's**, because the branch
  ruleset requires every review thread to be resolved regardless of who opened it.
  It runs on the maintainer's personal subscription, so a top-level `@claude review`
  comment from the maintainer
  is the only thing that starts it — there is no general-purpose `@claude` bot in this
  repo, and any other mention (including one inside a review thread) does nothing.
  Treat its findings like any review comment: reply, and push a fix or say why one
  isn't needed.

AI coding agents get the same rules in machine-facing form from
[AGENTS.md](https://github.com/schubydoo/clauster/blob/main/AGENTS.md) (Claude Code additionally reads [CLAUDE.md](https://github.com/schubydoo/clauster/blob/main/CLAUDE.md)).
If you change a workflow or gate, update those files in the same PR so they don't
drift.

## Documentation

Project docs live under [`docs/`](docs/index.md) (published with MkDocs). When
working on operability or deployment, the [Operations runbook](docs/operations.md)
collects health checks (`/healthz`, `/metrics`), `clauster doctor`, crash alerts,
reading the bridge debug log, the `KillMode` restart caveat, and backup/recovery.

## Vendoring front-end assets

Clauster **self-hosts** its front-end dependencies (no CDN) so the dashboard works
on an air-gapped / loopback deploy and `script-src` / `connect-src` stay `'self'`.
Vendored assets live under [`src/clauster/static/vendor/<dep>/`](https://github.com/schubydoo/clauster/tree/main/src/clauster/static/vendor)
(Alpine is the one exception — it sits flat at `static/alpine.csp.min.js`). To add or update one:

1. **Fetch the published dist** — `npm pack <pkg>@<version>` then `tar xzf` and copy the
   prebuilt files in (mirror an existing layout, e.g. `vendor/tabler/{css,js}/`). Don't
   add the package to `package.json` — there's no runtime `dependencies` block; the
   tarball is the source.
2. **Pin it for Renovate** — add a two-line block to
   [`static/vendor/versions.txt`](https://github.com/schubydoo/clauster/blob/main/src/clauster/static/vendor/versions.txt) in the exact
   shape the `customManager` regex matches:

   ```text
   # renovate: datasource=npm depName=<npm-name>
   <short-name>=<version>
   ```

   The existing `vendored-assets` `packageRule` (`renovate.json`) then tracks it —
   Renovate opens a **heads-up PR** on a new upstream but **never auto-merges** a
   vendored asset (it's labelled `vendored-assets` for a manual dist re-vendor). No
   `renovate.json` edit is needed.
3. **License + provenance** — copy the upstream `LICENSE` to `vendor/<dep>/LICENSE` and
   write a `vendor/<dep>/README.md` (package, version, a file→source table, and an
   `## Updating` recipe). When the asset is **shipped to users**, also add a section to
   [`THIRD_PARTY_NOTICES.md`](https://github.com/schubydoo/clauster/blob/main/THIRD_PARTY_NOTICES.md) and credit it in the dashboard
   footer (guarded by `test_dashboard_footer_credits_vendored_assets`).
4. **Reference it cache-busted** — link it in the template with `?v={{ asset_version }}`
   (the clauster version), so an upgrade busts the `immutable` static cache. Tests in
   `tests/test_app.py` assert the `?v=` pattern for shipped assets.

## License

By contributing, you agree that your contributions are licensed under the
project's [Apache-2.0 License](https://github.com/schubydoo/clauster/blob/main/LICENSE).
