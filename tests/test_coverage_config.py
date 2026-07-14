"""Guard: the Windows coverage config stays a superset of the base (#929).

The Windows CI job measures coverage through `.coveragerc-win` instead of pyproject's
`[tool.coverage]`, because Windows physically can't run clauster's POSIX code + its
ConPTY keeper is fake-seam-tested on Linux. That config MUST keep pyproject's base
`omit` / `exclude_also` and only ADD to them (the pty_keeper omit + the `skip-on-win`
tag) — otherwise the Windows floor would measure a different surface than Linux/macOS.
This fails loudly if the two drift.
"""

from __future__ import annotations

import configparser
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _base_coverage() -> dict:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["coverage"]


def _win_config() -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    read = cp.read(_ROOT / ".coveragerc-win", encoding="utf-8")
    assert read, ".coveragerc-win is missing — the Windows CI job needs it (#929)"
    return cp


def _multiline(value: str) -> set[str]:
    """Split a coverage config multi-line value into its entries (drop blanks/comments)."""
    return {
        ln.strip()
        for ln in value.strip().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    }


def test_win_omit_is_superset_of_base() -> None:
    base = set(_base_coverage()["run"]["omit"])
    win = _multiline(_win_config()["run"]["omit"])
    assert base <= win, (
        f"pyproject [tool.coverage.run] omit not all in .coveragerc-win: {base - win}"
    )
    # Windows additionally drops the pty keeper (POSIX keeper + fake-seam ConPTY runtime).
    assert any("pty_keeper" in entry for entry in win), "Windows config must omit pty_keeper"


def test_win_exclude_also_is_superset_of_base() -> None:
    base = set(_base_coverage()["report"]["exclude_also"])
    win = _multiline(_win_config()["report"]["exclude_also"])
    assert base <= win, (
        f"pyproject [tool.coverage.report] exclude_also not all in .coveragerc-win: {base - win}"
    )
    # The in-source POSIX-only tag, active on Windows only.
    assert "skip-on-win" in win, "Windows config must exclude the `skip-on-win` tag"
