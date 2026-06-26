"""Tests for scripts/lint_changesets.py — the changeset single-line-bullet guard (#599)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "lint_changesets.py"
_spec = importlib.util.spec_from_file_location("lint_changesets", _SCRIPT)
assert _spec is not None and _spec.loader is not None
lint_changesets = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint_changesets)


def _violation(raw: str) -> str | None:
    """Run the linter's body extractor + violation check on a full changeset string."""
    return lint_changesets._violation("x.md", lint_changesets._body_after_frontmatter(raw))


def test_single_line_body_passes() -> None:
    assert _violation("---\ndefault: patch\n---\n\nFix the thing, all on one clean line.") is None


def test_multi_paragraph_body_rejected() -> None:
    # The #599 breakage: a blank-line-separated "Upgrade note" makes knope render the entry
    # as a #### heading block instead of a bullet.
    body = "---\ndefault: minor\n---\n\nAdd a feature that does X.\n\nUpgrade note: also do Y."
    msg = _violation(body)
    assert msg is not None
    assert "multiple lines" in msg


def test_multiline_with_complete_first_line_now_rejected() -> None:
    # Previously ALLOWED (line 1 ended with '.', deemed a "clean heading"); now rejected —
    # a heading among bullets is itself the breakage this guard exists to prevent.
    body = "---\ndefault: patch\n---\n\nA complete summary sentence.\nTrailing detail line."
    assert _violation(body) is not None


def test_empty_body_rejected() -> None:
    assert _violation("---\ndefault: patch\n---\n") is not None
