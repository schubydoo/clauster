"""Tests for scripts/inject_highlights.py — the release-highlights folder."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "inject_highlights.py"
_spec = importlib.util.spec_from_file_location("inject_highlights", _SCRIPT)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _hl(body: str, for_version: str = "1.0.0") -> str:
    """Wrap a highlights ``body`` with the ``<!-- for: X.Y.Z -->`` marker knope keys on."""
    return f"<!-- for: {for_version} -->\n\n{body}"


def _changelog(*versions: str) -> str:
    """Build a knope-shaped changelog (heading + compare link + a Fixes section)."""
    blocks = ["# Changelog", ""]
    for v in versions:
        blocks += [
            f"## {v} (2026-07-16)",
            "",
            f"[Compare with prev](https://x/compare/...v{v})",
            "",
            "### Fixes",
            "",
            f"- Something in {v}",
            "",
        ]
    return "\n".join(blocks).rstrip("\n") + "\n"


def test_inserts_block_above_first_change_section() -> None:
    out = mod.insert_highlights(_changelog("1.0.0", "0.12.9"), "1.0.0", _hl("Big new thing."))
    lines = out.split("\n")
    assert lines[2] == "## 1.0.0 (2026-07-16)"
    assert lines[4].startswith("[Compare with ")
    assert mod.START in out
    assert "### Highlights" in out
    assert "Big new thing." in out
    hi = lines.index("### Highlights")
    fixes = lines.index("### Fixes")
    assert hi < fixes  # highlights lead the detailed changes
    assert out.count("### Highlights") == 1


def test_empty_highlights_is_noop() -> None:
    text = _changelog("1.0.0", "0.12.9")
    assert mod.insert_highlights(text, "1.0.0", _hl("   \n  ")) == text


def test_marker_for_other_version_is_noop() -> None:
    # The staleness guard: highlights written for 0.12.9 must not fold into a 1.0.0 release.
    text = _changelog("1.0.0", "0.12.9")
    assert mod.insert_highlights(text, "1.0.0", _hl("Old highlights.", "0.12.9")) == text


def test_missing_marker_is_noop() -> None:
    # Content with no `for:` marker never injects — the marker is the opt-in per version.
    text = _changelog("1.0.0")
    assert mod.insert_highlights(text, "1.0.0", "Unmarked content, no for-marker.") == text


def test_leading_comment_and_marker_are_stripped() -> None:
    src = _hl("The real highlight.")  # marker + note live in the leading comment
    out = mod.insert_highlights(_changelog("1.0.0"), "1.0.0", src)
    assert "The real highlight." in out
    assert "for: 1.0.0" not in out  # the marker comment is stripped from the body


def test_idempotent_second_run_is_noop() -> None:
    once = mod.insert_highlights(_changelog("1.0.0"), "1.0.0", _hl("Highlights here."))
    twice = mod.insert_highlights(once, "1.0.0", _hl("Highlights here."))
    assert twice == once


def test_rerun_after_body_change_replaces_block() -> None:
    # An edited HIGHLIGHTS.md must reconcile — replace the already-injected block, not skip.
    once = mod.insert_highlights(_changelog("1.0.0"), "1.0.0", _hl("First highlights."))
    assert "First highlights." in once
    twice = mod.insert_highlights(once, "1.0.0", _hl("Revised highlights."))
    assert "Revised highlights." in twice
    assert "First highlights." not in twice
    assert twice.count("### Highlights") == 1  # replaced in place, not duplicated


def test_version_mismatch_fails_loud() -> None:
    # Marked for 0.13.0 but the changelog top is 1.0.0 → highlights apply, shape is wrong.
    with pytest.raises(ValueError, match="expected '0.13.0'"):
        mod.insert_highlights(_changelog("1.0.0", "0.12.9"), "0.13.0", _hl("x", "0.13.0"))


def test_no_heading_fails_loud() -> None:
    with pytest.raises(ValueError, match="no `## X.Y.Z"):
        mod.insert_highlights("# Changelog\n\nNothing yet.\n", "1.0.0", _hl("x"))


def test_section_without_subsections_still_inserts() -> None:
    text = "# Changelog\n\n## 1.0.0 (2026-07-16)\n\n- A simple change\n"
    out = mod.insert_highlights(text, "1.0.0", _hl("Highlights."))
    lines = out.split("\n")
    assert lines.index("### Highlights") < lines.index("- A simple change")


def test_cli_writes_file(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(_changelog("1.0.0", "0.12.9"), encoding="utf-8")
    highlights = tmp_path / "HIGHLIGHTS.md"
    highlights.write_text(_hl("The headline feature.\n"), encoding="utf-8")
    rc = mod.main(["inject_highlights.py", "1.0.0", str(changelog), str(highlights)])
    assert rc == 0
    body = changelog.read_text(encoding="utf-8")
    assert "### Highlights" in body
    assert "The headline feature." in body


def test_cli_stale_marker_warns_and_is_noop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    original = _changelog("1.0.0")
    changelog.write_text(original, encoding="utf-8")
    highlights = tmp_path / "HIGHLIGHTS.md"
    highlights.write_text(_hl("Stale highlights.", "0.12.9"), encoding="utf-8")
    rc = mod.main(["inject_highlights.py", "1.0.0", str(changelog), str(highlights)])
    assert rc == 0
    assert changelog.read_text(encoding="utf-8") == original
    assert "::warning::" in capsys.readouterr().out


def test_cli_missing_highlights_file_is_noop(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    original = _changelog("1.0.0")
    changelog.write_text(original, encoding="utf-8")
    rc = mod.main(["inject_highlights.py", "1.0.0", str(changelog), str(tmp_path / "none.md")])
    assert rc == 0
    assert changelog.read_text(encoding="utf-8") == original


def test_cli_bad_args_returns_2() -> None:
    assert mod.main(["inject_highlights.py"]) == 2
