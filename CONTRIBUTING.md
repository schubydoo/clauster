# Contributing

Thanks for your interest in Clauster!

## Dev setup

```sh
uv sync --extra dev
cp clauster.yml.example clauster.yml   # edit projects_root
uv run clauster
```

## Before opening a PR

- **Tests + coverage** — `uv run pytest`; the suite must pass and stay at or
  above the **96%** coverage gate (enforced in CI).
- **Lint, format, types** —
  `uv run ruff check . && uv run ruff format --check . && uv run pyright src/clauster`
- **Security checks** — nothing extra to run locally: Bandit-style SAST is ruff's
  `S` rules, already covered by the `ruff check .` above. CI additionally runs
  CodeQL, Trivy (filesystem + image), dependency review, and a workflow audit
  (zizmor).
- **Conventional Commits** — your PR **title** must follow
  [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `docs:`, `chore:`, `ci:`, …). PRs are squash-merged, so the
  title becomes the commit subject that release-please parses for versioning and
  the changelog. CI enforces this.

CI runs the test suite on **Linux, macOS, and Windows** across Python 3.11–3.14
(all merge-blocking; Linux additionally enforces the 96% coverage gate), plus
lint, security scanners, and dependency review.

## License

By contributing, you agree that your contributions are licensed under the
project's [Apache-2.0 License](LICENSE).
