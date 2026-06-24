#!/usr/bin/env python3
"""Lint ``.changeset/*.md`` fragments so a wrapped summary can't truncate in release notes.

knope renders each changeset by taking its FIRST line as the entry summary and the rest as
details, then picking the first matching ``change_templates`` entry (knope.toml). A
multi-line body therefore renders as a ``### heading`` block whose heading is *line 1 only*
-- so a changeset whose first sentence wraps across lines ships a heading truncated
mid-sentence in the release notes (v0.12.3's #560/#561 did exactly this).

Rule: if a changeset body spans more than one line, the FIRST line must end with
sentence-terminating punctuation (``. ! ? :``) -- i.e. be a complete summary. A single-line
body (renders as a bullet) and a multi-line body whose first line is a full sentence
(renders as a clean heading) both pass.

Exit 0 when every fragment is well-formed, 1 (with a per-file reason) otherwise. Stdlib only.
"""

from __future__ import annotations

import glob

_CHANGESET_GLOB = ".changeset/*.md"
_TERMINATORS = (".", "!", "?", ":")


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
    # "Has details" = ANY non-blank line after the summary, even across a blank-line
    # paragraph break: knope splits on the first newline, so a blank-separated body still
    # renders line 1 as the heading. Checking only the adjacent line would miss that shape.
    multiline = any(line.strip() for line in content[1:])
    if multiline and not first.endswith(_TERMINATORS):
        return (
            f"{path}: the summary (line 1) wraps mid-sentence -- knope uses line 1 as the "
            f"entry summary, so the release-notes heading would truncate here:\n"
            f'      "{first}"\n'
            f"    Fix: keep the summary on ONE line, or end line 1 as a complete sentence "
            f"(. ! ? :) before continuing details on the next line."
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
