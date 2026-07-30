# You are reading the docs source, not the docs

GitHub renders this file above the directory listing; MkDocs never sees it. That
makes it the one place to say something to people browsing the repo that should
*not* appear on the published site.

**These pages track `main`, so they can describe behaviour that has not shipped yet.**
The published site is versioned ([#1084][1084]) and does not have that problem — it
defaults to the newest release:

**<https://schubydoo.github.io/clauster/>** — the newest release (the `latest`
alias). This is where to send anyone who asks for "the docs".

**<https://schubydoo.github.io/clauster/dev/>** — the published rendering of *these*
pages, rebuilt from `main`. Opt-in via the version selector, and it carries a banner
saying it is ahead of the release.

For what is actually in the version you installed, the release notes for that tag
remain the authority — see [Releases][releases] and [CHANGELOG.md][changelog].
Behaviour that changed in an unreleased version is marked inline where we have caught
it, with a `New in X.Y` / `Changed in X.Y` note (a convention adopted in [#995][995];
older pages predate it).

[releases]: https://github.com/schubydoo/clauster/releases
[changelog]: https://github.com/schubydoo/clauster/blob/main/CHANGELOG.md
[995]: https://github.com/schubydoo/clauster/issues/995
[1084]: https://github.com/schubydoo/clauster/issues/1084

---

Editing these pages? A few conventions that are easy to trip over:

- **Config reference tables are generated** from the pydantic models. Edit the
  model's `description=` in `src/clauster/config.py`, then run
  `scripts/gen_config_reference.py`; editing the rendered table by hand is undone
  on the next run and fails `just check`.
- **The site build is a gate.** `mkdocs build --strict` runs in `just check` and in
  CI. A broken internal link passes the markdown linter and fails only there.
- **Prefer linking over restating.** A fact repeated in a second file becomes a
  second cache, and the two disagree after the next change.

`index.md` — not this file — is the site's home page.
