"""The generated config-reference tables in the docs stay in sync.

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
EDITOR_DOC = ROOT / "docs" / "guides" / "config-editor.md"


def test_config_reference_docs_are_up_to_date():
    result = subprocess.run(
        [sys.executable, "scripts/gen_config_reference.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "docs/reference/config.md or docs/guides/config-editor.md is out of sync "
        "with the config models. Run: python scripts/gen_config_reference.py\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def _parse_tier_a_table() -> set[str]:
    """Parse the hand-written Tier-A allowlist table into a set of ``section.key`` paths.

    Locates the table by its ``editable_fields`` BEGIN/END GEN markers (not headings or
    line numbers) so it survives edits above it AND can't scoop up the adjacent Tier-B
    "Advanced fields" table, then reads ``| `section` | `k1`, `k2`, ... |`` rows into
    dotted paths.
    """
    text = EDITOR_DOC.read_text(encoding="utf-8")
    # The table is generated inside the `editable_fields` BEGIN/END markers (so the
    # `--check` gate covers it too); this independent parse is a belt-and-suspenders
    # cross-check that the RENDERED table equals `EDITABLE_FIELDS`, via a different code
    # path than the generator. Bounding by the markers keeps it from picking up the
    # separate `tier_b_fields` table that now sits just below. A missing marker means the
    # page was restructured — fail clearly instead of a bare ValueError traceback.
    try:
        start = text.index("<!-- BEGIN GEN: editable_fields -->")
        end = text.index("<!-- END GEN: editable_fields -->", start)
    except ValueError:
        pytest.fail(
            "editable_fields GEN markers not found — did docs/guides/config-editor.md "
            "structure change?"
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
            # A top-level (un-prefixed) field — e.g. `log_format` — is grouped under the
            # "(top-level)" section marker and parses to the bare dotted path with no prefix,
            # matching its literal `EDITABLE_FIELDS` entry.
            fields.add(key if section == "(top-level)" else f"{section}.{key}")
    return fields


def test_tier_a_allowlist_table_matches_editable_fields():
    """The rendered Tier-A table equals ``EDITABLE_FIELDS`` (independent of the gen gate)."""
    doc_fields = _parse_tier_a_table()
    code_fields = set(EDITABLE_FIELDS)
    assert doc_fields == code_fields, (
        "The Tier-A allowlist table in docs/guides/config-editor.md "
        "has drifted from config_editor.EDITABLE_FIELDS.\n"
        f"In code but missing from the doc table: {sorted(code_fields - doc_fields)}\n"
        f"In the doc table but not in code: {sorted(doc_fields - code_fields)}"
    )
