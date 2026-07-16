#!/usr/bin/env python3
"""Fold ``HIGHLIGHTS.md`` into the newest CHANGELOG version section (and the release body).

knope's ``Release`` step publishes the GitHub release with the *same* notes it wrote to
``CHANGELOG.md`` for that version, so a hand-curated "Highlights" summary reaches the
release body simply by living in that version's changelog section. This script runs as a
Command step right after ``changelog_compare_link.py`` (see ``knope.toml``): when
``HIGHLIGHTS.md`` is marked for the release being prepared, it inserts a ``### Highlights``
block just above the first change subsection of the newest version, wrapped in HTML-comment
markers so a re-run is a no-op.

Lifecycle & the version marker: ``HIGHLIGHTS.md`` carries a ``<!-- for: X.Y.Z -->`` marker
naming the release its content is written for. Highlights inject **only** when that marker
equals the version being prepared -- so a forgotten "update the highlights" step can never
fold a prior version's highlights into the next (immutable) release body. To ship highlights
for a release, curate the body and set ``for:`` to that version; leave the file empty (or the
marker pointing at an old version) and this is a no-op, so ordinary point releases just get
knope's plain generated notes.

Usage: ``inject_highlights.py <version> [changelog_path] [highlights_path]`` (knope passes
``$version``). Exits non-zero only on an unexpected changelog shape when highlights DO apply
-- fail loud, never ship a mangled release note. Stdlib only.
"""

import re
import sys

HEADING_RE = re.compile(r"^## (?P<version>\d+\.\d+\.\d+) \(\d{4}-\d{2}-\d{2}\)\s*$")
FOR_RE = re.compile(r"<!--.*?\bfor:\s*(?P<version>\d+\.\d+\.\d+).*?-->", re.DOTALL)
START = "<!-- highlights:start -->"
END = "<!-- highlights:end -->"


def body_of(highlights: str) -> str:
    """Return the injectable body: ``highlights`` minus its leading HTML-comment block.

    The leading ``<!-- ... -->`` block carries the ``for:`` marker + maintainer notes and
    must not leak into the release body, so it is stripped.
    """
    return re.sub(r"\A\s*<!--.*?-->\s*", "", highlights, count=1, flags=re.DOTALL).strip()


def target_version(highlights: str) -> str | None:
    """Return the ``<!-- for: X.Y.Z -->`` version these highlights target, or ``None``."""
    m = FOR_RE.search(highlights)
    return m.group("version") if m else None


def insert_highlights(text: str, version: str, highlights: str) -> str:
    """Return ``text`` with a ``### Highlights`` block folded into the newest version section.

    Injects only when ``highlights`` has a body AND is marked ``<!-- for: <version> -->`` for
    this exact release (the staleness guard); otherwise returns ``text`` unchanged. Also a
    no-op when the block is already present (idempotent). Raises ``ValueError`` when the
    highlights apply but the top heading is missing or doesn't match ``version`` -- an
    unexpected shape worth failing loudly on rather than silently mangling.
    """
    body = body_of(highlights)
    if not body or target_version(highlights) != version:
        return text

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

    section_end = heads[1] if len(heads) > 1 else len(lines)
    if any(START in ln for ln in lines[top:section_end]):
        return text  # already injected -- idempotent no-op

    # Insert just above the first change subsection/bullet, i.e. after the heading and its
    # compare-link line, so Highlights lead the section's detailed changes.
    insert_at = section_end
    for i in range(top + 1, section_end):
        if lines[i].startswith("### ") or lines[i].startswith("- "):
            insert_at = i
            break

    block = [START, "", "### Highlights", "", body, "", END, ""]
    lines[insert_at:insert_at] = block
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    """CLI entry: ``inject_highlights.py <version> [changelog_path] [highlights_path]``."""
    if not 2 <= len(argv) <= 4:
        print(__doc__)
        return 2
    version = argv[1]
    changelog_path = argv[2] if len(argv) >= 3 else "CHANGELOG.md"
    highlights_path = argv[3] if len(argv) == 4 else "HIGHLIGHTS.md"

    try:
        with open(highlights_path, encoding="utf-8") as fh:
            highlights = fh.read()
    except FileNotFoundError:
        print(f"no {highlights_path}; nothing to inject")
        return 0

    with open(changelog_path, encoding="utf-8") as fh:
        text = fh.read()
    updated = insert_highlights(text, version, highlights)
    if updated != text:
        with open(changelog_path, "w", encoding="utf-8") as fh:
            fh.write(updated)
        print(f"folded HIGHLIGHTS.md into {version} in {changelog_path}")
    elif body_of(highlights) and target_version(highlights) != version:
        # Content is present but marked for a different (or no) version — warn loudly so a
        # stale marker doesn't silently drop the highlights from a release the maintainer
        # meant to include them in.
        found = target_version(highlights) or "no `for:` marker"
        print(
            f"::warning::HIGHLIGHTS.md has content but targets {found}, not {version}; "
            f"skipping. Set `<!-- for: {version} -->` to include these highlights."
        )
    else:
        print(f"no change: highlights empty or already present for {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
