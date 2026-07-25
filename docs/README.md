# You are reading the docs source, not the docs

GitHub renders this file above the directory listing; MkDocs never sees it. That
makes it the one place to say something to people browsing the repo that should
*not* appear on the published site.

**These pages track `main`.** So does the published site — it is rebuilt from `main`
on every push and is not versioned, so both can describe behaviour that has not
shipped yet. For a nicer rendering with search, read:

**<https://schubydoo.github.io/clauster/>**

For what is actually in the version you installed, the authority is the release
notes for that tag — see [Releases][releases] and [CHANGELOG.md][changelog], not
these pages. Behaviour that changed in an unreleased version is marked inline where
we have caught it, with a `New in X.Y` / `Changed in X.Y` note (a convention adopted
in [#995][995]; older pages predate it).

[releases]: https://github.com/schubydoo/clauster/releases
[changelog]: https://github.com/schubydoo/clauster/blob/main/CHANGELOG.md
[995]: https://github.com/schubydoo/clauster/issues/995

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
