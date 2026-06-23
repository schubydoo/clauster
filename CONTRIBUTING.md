# Contributing

Thanks for your interest in Clauster!

## Code of conduct

This project adheres to the [Contributor Covenant](CODE_OF_CONDUCT.md) code of
conduct. By participating, you are expected to uphold it; please report
unacceptable behavior as described there.

## Dev setup

```sh
uv sync --extra dev
npm ci                                 # markdownlint-cli2 (pinned, integrity-checked)
uv run pre-commit install              # auto-lint on commit (ruff + yaml/markdown)
cp clauster.yml.example clauster.yml   # edit projects_root
uv run clauster
```

## Before opening a PR

- **Tests + coverage** — `uv run pytest`; the suite must pass and stay at or
  above the **96%** coverage gate (enforced in CI).
- **Lint, format, types** —
  `uv run ruff check . && uv run ruff format --check . && uv run pyright src/clauster`
- **Docs lint (Markdown + YAML)** — `bash scripts/lint-docs.sh` (markdownlint-cli2
  from `npm ci`, pinned in `package.json`; `yamllint` from `uv sync --extra dev`).
  Same command CI runs in the `lint` job.
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
  You don't need to add a PR link — knope appends `([#NNN])` for the PR that
  introduces the changeset, at release time.
  Internal-only PRs (CI, refactor, tests, docs) need no changeset — add the
  `no-changelog` label to silence the advisory `Changeset check`. **Don't hand-edit
  `CHANGELOG.md` or bump the version in `pyproject.toml` / `src/clauster/__init__.py`**
  — knope owns those, regenerating them in the release PR.

  On same-repo PRs that touch `src/` and lack a changeset, a bot may **auto-draft**
  one for you (the `changeset-autodraft` workflow) and push it to your branch. Treat
  it as a starting point, not the final word: open the diff, confirm the bump type
  (`patch` / `minor` / `major` / `perf` / `security` / `build`) and the summary
  sentence match what your change actually does, and adjust them if not. It's a
  **DRAFT** guessed from the diff — don't trust it blindly. (Fork PRs get the
  advisory `Changeset check` nag instead and should add the changeset by hand.)

CI runs the test suite on **Linux, macOS, and Windows** across Python 3.11–3.14
(all merge-blocking; Linux additionally enforces the 96% coverage gate), plus
lint, security scanners, and dependency review.

## Documentation

Project docs live under [`docs/`](docs/index.md) (published with MkDocs). When
working on operability or deployment, the [Operations runbook](docs/operations.md)
collects health checks (`/healthz`, `/metrics`), `clauster doctor`, crash alerts,
reading the bridge debug log, the `KillMode` restart caveat, and backup/recovery.

## License

By contributing, you agree that your contributions are licensed under the
project's [Apache-2.0 License](LICENSE).
