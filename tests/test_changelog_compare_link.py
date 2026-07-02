"""Tests for scripts/changelog_compare_link.py — the post-release compare-link inserter."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "changelog_compare_link.py"
_spec = importlib.util.spec_from_file_location("changelog_compare_link", _SCRIPT)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

_REPO = "https://github.com/schubydoo/clauster"


def _changelog(*versions: str) -> str:
    """Build a knope-shaped changelog (plain headings, no compare links) for ``versions``."""
    blocks = ["# Changelog", ""]
    for v in versions:
        blocks += [f"## {v} (2026-07-02)", "", "### Fixes", "", f"- Something in {v}", ""]
    return "\n".join(blocks).rstrip("\n") + "\n"


def test_inserts_link_under_newest_heading() -> None:
    out = mod.insert_compare_link(_changelog("0.12.10", "0.12.9"), "0.12.10")
    lines = out.split("\n")
    assert lines[0] == "# Changelog"
    assert lines[2] == "## 0.12.10 (2026-07-02)"
    assert lines[3] == ""
    assert lines[4] == f"[Compare with 0.12.9]({_REPO}/compare/v0.12.9...v0.12.10)"
    assert lines[5] == ""
    assert lines[6] == "### Fixes"
    # only the newest section gets a link; the older one is left untouched
    assert out.count("[Compare with ") == 1


def test_idempotent_second_run_is_noop() -> None:
    once = mod.insert_compare_link(_changelog("0.12.10", "0.12.9"), "0.12.10")
    twice = mod.insert_compare_link(once, "0.12.10")
    assert twice == once


def test_first_release_ever_is_noop() -> None:
    text = _changelog("0.1.0")
    assert mod.insert_compare_link(text, "0.1.0") == text


def test_version_mismatch_fails_loud() -> None:
    # Guards against running before PrepareRelease wrote the new heading.
    with pytest.raises(ValueError, match="expected '0.13.0'"):
        mod.insert_compare_link(_changelog("0.12.10", "0.12.9"), "0.13.0")


def test_no_heading_fails_loud() -> None:
    with pytest.raises(ValueError, match="no `## X.Y.Z"):
        mod.insert_compare_link("# Changelog\n\nNothing here yet.\n", "0.1.0")


def test_cli_writes_file(tmp_path: Path) -> None:
    p = tmp_path / "CHANGELOG.md"
    p.write_text(_changelog("0.12.10", "0.12.9"), encoding="utf-8")
    assert mod.main(["changelog_compare_link.py", "0.12.10", str(p)]) == 0
    body = p.read_text(encoding="utf-8")
    assert f"[Compare with 0.12.9]({_REPO}/compare/v0.12.9...v0.12.10)" in body


def test_cli_bad_args_returns_2() -> None:
    assert mod.main(["changelog_compare_link.py"]) == 2
