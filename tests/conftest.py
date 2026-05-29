from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    """A projects_root with a git project, a CLAUDE.md project, a plain one,
    a dotdir, and a bad-name dir (should be skipped)."""
    (tmp_path / "alpha" / ".git").mkdir(parents=True)
    beta = tmp_path / "beta"
    beta.mkdir()
    (beta / "CLAUDE.md").write_text("# beta\n")
    (tmp_path / "gamma").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "bad name!").mkdir()  # invalid project name -> skipped
    return tmp_path


@pytest.fixture
def write_config(tmp_path: Path, projects_root: Path):
    def _write(extra: str = "") -> Path:
        cfg = tmp_path / "clauster.yml"
        cfg.write_text(f"projects_root: {projects_root}\n{extra}")
        return cfg

    return _write
