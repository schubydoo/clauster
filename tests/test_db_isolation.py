"""Regression guard: tests must never resolve onto a real ``~/.clauster`` DB.

The persistence DB path comes from ``Config.state_dir`` (default ``~/.clauster``,
expanded via ``Path.expanduser()``), and startup runs Alembic ``upgrade(..., "head")``
against ``<state_dir>/clauster.db``. Without HOME isolation a test that builds a
default config would migrate the developer's *real* database — which actually
happened once. The autouse ``_isolate_clauster_home`` fixture in ``conftest.py``
redirects HOME per test; these tests assert that redirection actually holds, so the
gap can't silently return.
"""

from __future__ import annotations

import os
from pathlib import Path

from clauster.config import ClausterConfig
from clauster.db.engine import DB_FILENAME, resolve_url

# The developer's REAL home, captured at import time. conftest imports before this
# module is collected, but the autouse fixture only mutates os.environ *inside* each
# test (function scope), so at module-import the environment is still the real one.
_REAL_HOME = Path(os.path.expanduser("~")).resolve()


def test_home_env_is_redirected_under_a_temp_dir():
    """HOME points somewhere other than the developer's real home."""
    redirected = Path(os.environ["HOME"]).resolve()
    assert redirected != _REAL_HOME
    # ``~`` must now expand to the redirected temp HOME, not the real one.
    assert Path("~").expanduser().resolve() == redirected


def test_default_clauster_home_resolves_off_the_real_home():
    """``~/.clauster`` expands under the temp HOME, never under the real home."""
    resolved = Path("~/.clauster").expanduser().resolve()
    assert resolved != (_REAL_HOME / ".clauster")
    assert _REAL_HOME not in resolved.parents


def test_default_config_state_dir_and_db_are_isolated():
    """A default ``ClausterConfig`` resolves its state_dir + SQLite DB off real home."""
    cfg = ClausterConfig(projects_root=Path.cwd())
    state_dir = cfg.state_dir.expanduser().resolve()
    # The default state_dir is ``~/.clauster`` — it must sit under the temp HOME.
    assert state_dir == Path("~/.clauster").expanduser().resolve()
    assert _REAL_HOME not in state_dir.parents
    assert state_dir != (_REAL_HOME / ".clauster")

    # The SQLite URL the engine would open must point off the real home too, so an
    # Alembic ``upgrade`` could never reach the real ``clauster.db``.
    db_path = (state_dir / DB_FILENAME).resolve()
    assert _REAL_HOME not in db_path.parents
    assert resolve_url(cfg.state_dir, cfg.database_url).startswith("sqlite:///")
    assert str(_REAL_HOME / ".clauster" / DB_FILENAME) not in resolve_url(
        cfg.state_dir, cfg.database_url
    )


def test_stray_real_env_overrides_are_dropped(monkeypatch):
    """CLAUSTER_CONFIG / CLAUSTER_STATE_DIR from the real env are removed per test."""
    assert "CLAUSTER_CONFIG" not in os.environ
    assert "CLAUSTER_STATE_DIR" not in os.environ
