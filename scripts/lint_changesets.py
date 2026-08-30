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
    paths = sorted(str(p) for p in _CHANGESET_DIR.glob("*.md"))
    problems = []
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
