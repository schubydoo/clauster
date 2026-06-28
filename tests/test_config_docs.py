"""The generated config-reference tables in docs/configuration.md stay in sync.

Guards against the drift that prompted the generator: a config field added or
changed in ``config.py`` without regenerating the docs page. The generator is the
single source of truth (descriptions live in ``Field(description=...)``), so this
just runs it in ``--check`` mode and fails if the committed page is stale.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from clauster.config_editor import EDITABLE_FIELDS

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DOC = ROOT / "docs" / "configuration.md"


def test_config_reference_docs_are_up_to_date():
    result = subprocess.run(
        [sys.executable, "scripts/gen_config_reference.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "docs/configuration.md is out of sync with the config models. "
        "Run: python scripts/gen_config_reference.py\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def _parse_tier_a_table() -> set[str]:
    """Parse the hand-written Tier-A allowlist table into a set of ``section.key`` paths.

    Locates the table by its heading anchors (not line numbers) so it survives edits
    above it, then reads ``| `section` | `k1`, `k2`, ... |`` rows into dotted paths.
    """
    text = CONFIG_DOC.read_text(encoding="utf-8")
    # The table lives between these two headings (it sits OUTSIDE the gen-config
    # BEGIN/END markers, so the `--check` gate above does not cover it). A missing
    # heading means the page was restructured — fail clearly instead of a bare
    # ValueError traceback.
    try:
        start = text.index("### What's editable")
    except ValueError:
        pytest.fail(
            "Tier-A heading '### What's editable' not found — did "
            "docs/configuration.md headings change?"
        )
    try:
        end = text.index("### Why everything else", start)
    except ValueError:
        pytest.fail(
            "Tier-A heading '### Why everything else' not found — did "
            "docs/configuration.md headings change?"
        )
    fields: set[str] = set()
    for line in text[start:end].splitlines():
        if not line.startswith("|"):
            continue
        cols = [c.strip() for c in line.strip("|").split("|")]
        if len(cols) != 2:
            continue
        section_match = re.fullmatch(r"`([^`]+)`", cols[0])
        if section_match is None:  # header / separator row
            continue
        section = section_match.group(1)
        for key in re.findall(r"`([^`]+)`", cols[1]):
            fields.add(f"{section}.{key}")
    return fields


def test_tier_a_allowlist_table_matches_editable_fields():
    """The hand-written Tier-A table equals ``EDITABLE_FIELDS`` (the gen-config gate misses it)."""
    doc_fields = _parse_tier_a_table()
    code_fields = set(EDITABLE_FIELDS)
    assert doc_fields == code_fields, (
        "The 'What's editable — the Tier-A allowlist' table in docs/configuration.md "
        "has drifted from config_editor.EDITABLE_FIELDS.\n"
        f"In code but missing from the doc table: {sorted(code_fields - doc_fields)}\n"
        f"In the doc table but not in code: {sorted(doc_fields - code_fields)}"
    )
