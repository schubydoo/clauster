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
- **Conventional Commits** — your PR **title** must follow
  [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `docs:`, `chore:`, `ci:`, …). PRs are squash-merged, so the
  title becomes the commit subject that release-please parses for versioning and
  the changelog. CI enforces this.

CI runs tests (Linux on Python 3.11–3.14; macOS/Windows are informational for
now), lint, security scanners, and dependency review.

## License

By contributing, you agree that your contributions are licensed under the
project's [Apache-2.0 License](LICENSE).
