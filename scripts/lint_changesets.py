#!/usr/bin/env python3
"""Lint ``.changeset/*.md`` fragments so every entry renders as a clean changelog bullet.

Two independent rules, both enforced here:

**Frontmatter.** knope only consumes a fragment whose ``--- ... ---`` block names a package
it knows about, mapped to a change type it knows about. Verified against knope 0.23.0, the
three ways to get that wrong fail in three different ways, and only the last one is loud:

* an unknown **package** key (``clauster: patch`` rather than the single-package
  ``default: patch``) -- knope silently IGNORES the fragment: no error, no changelog entry,
  no version bump, and the file is left on disk. Three fragments shipped that way and were
  missing from the 1.1.0 release notes (#1320);
* an unknown **change type** (a typo, wrong case, a trailing ``#`` comment, or a *quoted*
  ``"patch"``) -- knope still bumps the version and still DELETES the fragment, but writes
  an empty changelog section. The entry is gone and the evidence with it;
* malformed frontmatter (absent, BOM-prefixed, unterminated, empty, blank line inside,
  duplicate key) -- knope fails the release run with ``missing``/``invalid front matter``.
  Loud, but only at release time, which is the worst moment to discover it.

The package key and the valid change types are read from ``knope.toml`` rather than
hardcoded, so this linter cannot drift from the config knope actually obeys.

**Body.** knope renders each changeset by taking its FIRST line as the entry summary; ANY further
content -- a second line, or a blank-line-separated paragraph like an "Upgrade note" --
makes knope render the entry as a ``#### heading`` block (the summary as the heading, the
rest as body) instead of a bullet. A lone ``####`` heading dropped into the middle of a
bulleted "Features"/"Fixes" list breaks the changelog: that is exactly what #617's
multi-paragraph changeset did to the v0.12.5 release notes (#599), and v0.12.3's #560/#561
hit the related mid-sentence-truncation flavour of the same single-line-summary rule.

Rule: a changeset body must be a SINGLE line -- it then always renders as a clean bullet.
Fold any details / "Upgrade note" into that one sentence; never span lines or add a second
paragraph. (Earlier this allowed a multi-line body whose line 1 was a complete sentence,
on the theory the heading was "clean" -- but a heading among bullets is itself the breakage,
so multi-line is now rejected outright.)

**Layout.** knope reads ``.changeset/*.md`` and nothing else, so a fragment that lands as
``a.txt``, ``a.MD`` or ``sub/a.md`` is dropped exactly as silently as a wrongly-keyed one --
no changelog entry, no version bump, and (before this rule) no lint failure either (#1332).
Every entry in ``.changeset/`` must therefore be a top-level ``*.md`` fragment this linter
can validate, bar the housekeeping names in ``_ALLOWED_NON_FRAGMENTS`` and the editor
scratch files in ``_EDITOR_ARTEFACT_SUFFIXES``. There is deliberately no ``README.md``
carve-out -- see the note in ``main``.

Exit 0 when every fragment is well-formed, 1 (with a per-file reason) otherwise. Stdlib only.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

# Anchored to the repo root rather than the cwd: a relative glob run from a subdirectory
# would find zero fragments and report a cheerful "0 fragment(s) OK" -- a silent pass, the
# very failure mode this script exists to close. `Path.glob` rather than `glob.glob`: the
# latter takes the WHOLE path as a pattern, so a checkout under a directory containing
# `[`, `]`, `*` or `?` would match nothing -- the same silent pass by another route.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHANGESET_DIR = _REPO_ROOT / ".changeset"
_KNOPE_TOML = str(_REPO_ROOT / "knope.toml")

# knope's built-in change types. `extra_changelog_sections` in knope.toml adds the
# repo's custom ones (perf / security / build), which are equally valid in a fragment.
_BUILTIN_CHANGE_TYPES = frozenset({"major", "minor", "patch"})

# The only names allowed in `.changeset/` that are not fragments. Deliberately a closed
# allowlist rather than a "skip dotfiles" rule: an unexpected name is far more likely to be
# a changeset that landed under the wrong extension than a housekeeping file worth keeping,
# and making that loud is this linter's whole job. (Note a dotted `*.md` name is NOT
# special -- knope consumes `.changeset/.hidden.md` and renders its entry, verified against
# 0.23.0, and `endswith(".md")` below matches it the same way.)
#
# * `.gitkeep` -- git cannot track an empty directory, so a contributor who wants
#   `.changeset/` to survive in the tree once knope has consumed every pending fragment
#   adds one. None is committed today; absent is equally fine (see `main`).
# * `.DS_Store` / `Thumbs.db` -- created by macOS Finder and Windows Explorer merely for
#   LOOKING in the folder. CI runs on clean checkouts, so failing on these would red only
#   local runs, on the two OSes the maintainer does not develop on.
_ALLOWED_NON_FRAGMENTS = frozenset({".gitkeep", ".DS_Store", "Thumbs.db"})

# Editor scratch files, which exist only WHILE a fragment is open: a `just check` run
# mid-edit must not go red for one. Vim's shapes specifically (`.a.md.swp` beside `a.md`,
# and `a.md~`) -- emacs' `#a.md#` is not covered, since a suffix test cannot express it and
# a stray called `#slug.md#` is a plausible enough mistake to keep failing loudly.
_EDITOR_ARTEFACT_SUFFIXES = (".swp", ".swo", "~")


def _knope_expectations(config_path: str) -> tuple[frozenset[str], frozenset[str]]:
    """Return the (package keys, change types) knope will accept, read from ``knope.toml``.

    A single unnamed ``[package]`` section -- clauster's layout -- makes knope name the
    package ``default``. A ``[packages.NAME]`` layout scopes ``extra_changelog_sections``
    per package, which this linter does not model, so it refuses one outright rather than
    guessing: better a loud failure than silently blessing a fragment knope will drop.
    """
    with open(config_path, "rb") as fh:
        config = tomllib.load(fh)
    if "packages" in config:
        raise ValueError(
            f"{config_path} uses the multi-package `[packages.NAME]` layout, which this "
            f"linter does not handle (change types are scoped per package there). Teach "
            f"_knope_expectations the per-package mapping before switching layouts."
        )
    package = config.get("package")
    if not isinstance(package, dict):
        raise ValueError(f"{config_path} has no `[package]` table to read the layout from.")
    types = set(_BUILTIN_CHANGE_TYPES)
    for section in package.get("extra_changelog_sections") or ():
        types.update(section.get("types") or ())
    return frozenset({"default"}), frozenset(types)


def _relative_to_cwd(path: str) -> str:
    """Shorten an absolute fragment path for the report; fall back to it when it can't be."""
    try:
        return os.path.relpath(path)
    except ValueError:  # Windows: cwd on a different drive than the repo
        return path


def _split_frontmatter(text: str) -> tuple[list[str] | None, str]:
    """Split a changeset into its ``--- ... ---`` frontmatter lines and the body below it.

    The frontmatter is ``None`` when the block is absent or unterminated -- knope would not
    read such a file as a changeset at all, so the caller reports it rather than guessing.
    """
    lines = text.splitlines()
    if not (lines and lines[0].strip() == "---"):
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[1:i], "\n".join(lines[i + 1 :])
    return None, ""  # unterminated frontmatter -> no usable body


def _frontmatter_violation(
    path: str, front: list[str] | None, packages: frozenset[str], change_types: frozenset[str]
) -> str | None:
    """Return a violation message for frontmatter knope would ignore, or None if it is fine."""
    expected = ", ".join(f"`{key}: patch`" for key in sorted(packages))
    if front is None:
        return (
            f"{path}: no readable `---` frontmatter block (absent, unterminated, or preceded "
            f"by a byte-order mark) -- knope fails the release run on it with `missing`/"
            f"`invalid front matter`. Fix: make the FIRST bytes of the file `---`, then "
            f"{expected}, then a closing `---`."
        )
    entries: dict[str, str] = {}
    for line in front:
        # No `continue` for a blank line: knope rejects one inside the block, so accepting
        # it here would just defer the failure to the release run.
        key, sep, value = line.partition(":")
        if not sep or not key.strip():
            return (
                f'{path}: unparsable frontmatter line "{line.strip()}" -- expected '
                f"`<package>: <bump>`, e.g. {expected}."
            )
        key = key.strip()
        if key in entries:
            return f"{path}: duplicate frontmatter key '{key}' -- knope rejects that."
        # Deliberately NOT stripping quotes: knope treats `default: "patch"` as an unknown
        # change type, bumps the version, DELETES the fragment and writes an empty changelog
        # section. A quoted value must fail here, not vanish at release time.
        entries[key] = value.strip()
    if not entries:
        return f"{path}: empty frontmatter block -- expected {expected}."
    unknown = sorted(set(entries) - packages)
    if unknown:
        # knope silently ignores a fragment whose keys it ALL fails to recognise (#1320); it
        # copes with a stray extra key alongside a valid one, but that is a typo either way,
        # so this is deliberately stricter than knope rather than a claim about its behaviour.
        return (
            f"{path}: frontmatter key(s) {', '.join(repr(k) for k in unknown)} are not "
            f"packages knope knows about. A fragment it cannot key is skipped SILENTLY -- no "
            f"changelog entry and no version bump (#1320). Fix: use {expected}."
        )
    bad = sorted((k, v) for k, v in entries.items() if v not in change_types)
    if bad:
        detail = ", ".join(f"`{k}: {v}`" for k, v in bad)
        return (
            f"{path}: change type(s) {detail} are not valid for this repo -- knope bumps the "
            f"version, deletes the fragment and writes an EMPTY changelog section. Use one "
            f"of (unquoted, lowercase): {', '.join(sorted(change_types))}."
        )
    return None


def _violation(path: str, body: str) -> str | None:
    """Return a violation message for a malformed changeset body, or None if it is fine."""
    content = body.splitlines()
    while content and content[0].strip() == "":
        content.pop(0)
    if not content:
        return f"{path}: empty changeset body (needs a one-line summary)."
    first = content[0].rstrip()
    # Reject ANY content after the summary line -- even across a blank-line paragraph
    # break (an "Upgrade note", a second sentence on its own line). knope splits on the
    # first newline and renders such a body as a `#### heading` block, not a bullet, which
    # breaks the uniform bulleted changelog (#599). A single-line body always renders as a
    # clean bullet.
    if any(line.strip() for line in content[1:]):
        return (
            f"{path}: changeset body spans multiple lines -- knope renders that as a "
            f"`#### heading` block, not a bullet, which breaks the changelog (#599). "
            f"Summary (line 1):\n"
            f'      "{first}"\n'
            f"    Fix: keep the WHOLE entry on ONE line so it renders as a clean bullet; "
            f"fold any details / 'Upgrade note' into that single sentence."
        )
    return None


def _is_housekeeping(name: str) -> bool:
    """Return True for the few non-fragment names allowed to sit in ``.changeset/``."""
    return name in _ALLOWED_NON_FRAGMENTS or name.endswith(_EDITOR_ARTEFACT_SUFFIXES)


def _stray_violation(path: str, is_dir: bool) -> str:
    """Return the violation message for an entry in ``.changeset/`` knope will never read."""
    what = (
        "a directory -- knope's `*.md` glob does not recurse, so a fragment nested inside it"
        if is_dir
        else "not a `*.md` fragment -- knope reads only `.changeset/*.md`, so it"
    )
    allowed = ", ".join(f"`{name}`" for name in sorted(_ALLOWED_NON_FRAGMENTS))
    suffixes = ", ".join(f"`{suffix}`" for suffix in _EDITOR_ARTEFACT_SUFFIXES)
    return (
        f"{path}: {what} is dropped SILENTLY -- no changelog entry and no version bump, and "
        f"nothing fails until the release notes come out short (#1332). Fix: make it a "
        f"top-level `.changeset/<slug>.md` fragment (the extension is matched "
        f"case-sensitively, so `.MD` counts as a stray), or delete it. The only "
        f"non-fragments allowed here: {allowed}, and editor scratch files ending {suffixes}."
    )


def main() -> int:
    """Lint every changeset fragment; print violations and return 1 if any, else 0."""
    try:
        packages, change_types = _knope_expectations(_KNOPE_TOML)
    # AttributeError/TypeError too: a knope.toml whose tables are the wrong SHAPE (say
    # `extra_changelog_sections = "oops"`) trips those, and a bare traceback loses the
    # message that tells the reader which file to look at.
    except (OSError, ValueError, AttributeError, TypeError) as exc:  # TOMLDecodeError < ValueError
        # Fail closed: without knope.toml we cannot say which frontmatter knope accepts,
        # and passing anyway is how a silently-skipped fragment got shipped in the first place.
        toml = _relative_to_cwd(_KNOPE_TOML)
        print(f"Changeset lint FAILED: could not read or interpret {toml} ({exc}).")
        return 1
    # Every `*.md` here, with no README exemption -- knope 0.23.0 has none either: it parses
    # a `.changeset/README.md` as a fragment and fails the release run on its missing
    # frontmatter. (knope-prepare.yml's `! -iname 'README.md'` only COUNTS pending fragments;
    # skipping it here too would hide that failure until it hit main.)
    #
    # `iterdir` rather than a `*.md` glob so the entries that DON'T match are visible: they
    # are the silent-drop case this rule closes (#1332). A missing `.changeset/` is not one
    # of them -- knope deletes every fragment as it prepares a release and git does not track
    # the emptied directory, so it is legitimately absent on a release checkout. Anything
    # else at that path is NOT legitimate: reporting "0 fragment(s) OK" for a `.changeset`
    # that is a regular file would be this script's own silent pass.
    cdir = _relative_to_cwd(str(_CHANGESET_DIR))
    # `is_symlink()` as well as `exists()`: the latter FOLLOWS the link, so a dangling
    # `.changeset -> /nowhere` would otherwise read as "absent" and pass.
    if (_CHANGESET_DIR.exists() or _CHANGESET_DIR.is_symlink()) and not _CHANGESET_DIR.is_dir():
        print(f"Changeset lint FAILED: {cdir} is not a directory (regular file? dead symlink?).")
        return 1
    try:
        entries = sorted(_CHANGESET_DIR.iterdir()) if _CHANGESET_DIR.is_dir() else []
    except OSError as exc:
        # Every other error path here is caught and named; an unreadable directory should
        # not be the one that exits on a bare traceback.
        print(f"Changeset lint FAILED: could not list {cdir} ({exc}).")
        return 1
    # Name-based, so a DIRECTORY called `foo.md` still reaches the read below and is reported
    # as unreadable rather than as a stray -- either way it fails, and the split keeps this
    # rule about layout only.
    paths = [str(p) for p in entries if p.name.endswith(".md")]
    problems = [
        _stray_violation(_relative_to_cwd(str(p)), p.is_dir())
        for p in entries
        if not p.name.endswith(".md") and not _is_housekeeping(p.name)
    ]
    for path in paths:
        label = _relative_to_cwd(path)
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            problems.append(f"{label}: could not read ({exc}).")
            continue
        front, body = _split_frontmatter(text)
        msg = _frontmatter_violation(label, front, packages, change_types) or _violation(
            label, body
        )
        if msg is not None:
            problems.append(msg)
    if problems:
        print("Changeset lint FAILED:\n")
        for problem in problems:
            print(f"  x {problem}\n")
        return 1
    print(f"Changeset lint: {len(paths)} fragment(s) OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
