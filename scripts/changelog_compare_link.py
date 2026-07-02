#!/usr/bin/env python3
"""Insert a ``[Compare with <prev>]`` link under the newest CHANGELOG heading.

knope owns the changelog version heading and writes it as a plain ``## X.Y.Z (date)``
line with no diff link (knope has no template for the heading itself, and a linked
``## [X.Y.Z](url)`` heading is one knope no longer recognizes as a release -- which is
exactly what silently mis-ordered this changelog after the release-please -> knope move).

So the compare link lives on its own line *under* the heading, where knope preserves it
untouched. This script runs as a Command step right after ``PrepareRelease`` (see
``knope.toml``): it finds the newest version section knope just wrote and inserts

    [Compare with <prev>](<REPO>/compare/v<prev>...v<version>)

between the heading and its first ``### `` section. It's idempotent (a second run is a
no-op) and a no-op for the very first release (no predecessor to compare against). The
``v<version>`` tag doesn't exist until the release publishes -- that's fine, it resolves
once the tag is pushed, exactly as release-please's links did.

Usage: ``changelog_compare_link.py <version> [changelog_path]`` (knope passes ``$version``).
Exits non-zero if the changelog shape is unexpected -- fail loud, never ship a wrong link.
Stdlib only.
"""

import re
import sys

REPO = "https://github.com/schubydoo/clauster"
HEADING_RE = re.compile(r"^## (?P<version>\d+\.\d+\.\d+) \(\d{4}-\d{2}-\d{2}\)\s*$")


def insert_compare_link(text: str, version: str) -> str:
    """Return ``text`` with a compare link added under the newest (top) version heading.

    Raises ``ValueError`` if the top version heading is missing or doesn't match
    ``version``. Returns ``text`` unchanged when the link already exists or there is no
    older version to compare against.
    """
    lines = text.split("\n")
    heads = [i for i, ln in enumerate(lines) if HEADING_RE.match(ln)]
    if not heads:
        raise ValueError("no `## X.Y.Z (date)` version heading found in changelog")

    top = heads[0]
    top_version = HEADING_RE.match(lines[top]).group("version")
    if top_version != version:
        raise ValueError(
            f"newest changelog heading is {top_version!r}, expected {version!r} -- "
            "did PrepareRelease run first?"
        )
    if len(heads) < 2:
        return text  # first release ever: nothing to compare against
    prev = HEADING_RE.match(lines[heads[1]]).group("version")

    link = f"[Compare with {prev}]({REPO}/compare/v{prev}...v{version})"
    # Heading is followed by a blank line; the compare link (if present) sits after it.
    if lines[top + 1 : top + 2] and lines[top + 1].startswith("[Compare with "):
        return text  # a stray link on the blank line -- unexpected, leave it alone
    if lines[top + 2 : top + 3] and lines[top + 2].startswith("[Compare with "):
        return text  # already inserted -- idempotent no-op
    lines[top + 2 : top + 2] = [link, ""]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    """CLI entry: ``changelog_compare_link.py <version> [changelog_path]``."""
    if not 2 <= len(argv) <= 3:
        print(__doc__)
        return 2
    version = argv[1]
    path = argv[2] if len(argv) == 3 else "CHANGELOG.md"
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    updated = insert_compare_link(text, version)
    if updated != text:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(updated)
        print(f"added compare link to {version} in {path}")
    else:
        print(f"no change: compare link already present or no predecessor for {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
