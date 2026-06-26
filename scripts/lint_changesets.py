#!/usr/bin/env python3
"""Lint ``.changeset/*.md`` fragments so every entry renders as a clean changelog bullet.

knope renders each changeset by taking its FIRST line as the entry summary; ANY further
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

import glob

_CHANGESET_GLOB = ".changeset/*.md"


def _body_after_frontmatter(text: str) -> str:
    """Return the changeset body with a leading ``--- ... ---`` frontmatter block removed."""
    lines = text.splitlines()
    if not (lines and lines[0].strip() == "---"):
        return text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1 :])
    return ""  # unterminated frontmatter -> no usable body


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
    paths = sorted(glob.glob(_CHANGESET_GLOB))
    problems = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            problems.append(f"{path}: could not read ({exc}).")
            continue
        msg = _violation(path, _body_after_frontmatter(text))
        if msg is not None:
            problems.append(msg)
    if problems:
        print("Changeset summary lint FAILED:\n")
        for problem in problems:
            print(f"  x {problem}\n")
        return 1
    print(f"Changeset summary lint: {len(paths)} fragment(s) OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
