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

import pytest

from clauster.config import ClausterConfig
from clauster.db.engine import DB_FILENAME, resolve_url

# The developer's REAL home. conftest pins HOME to a throwaway dir at its own import
# (before this module is collected), so ``expanduser("~")`` here would already yield
# that temp dir — conftest stashes the true home in CLAUSTER_TEST_REAL_HOME for us.
_REAL_HOME = Path(os.environ.get("CLAUSTER_TEST_REAL_HOME") or os.path.expanduser("~")).resolve()


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
    assert resolve_url(cfg.state_dir).startswith("sqlite:///")
    assert str(_REAL_HOME / ".clauster" / DB_FILENAME) not in resolve_url(cfg.state_dir)


@pytest.fixture(scope="module", autouse=True)
def _inject_stray_overrides():
    """Simulate a developer's real env carrying CLAUSTER_CONFIG / CLAUSTER_STATE_DIR.

    Without this the absence assertion below is tautological — it passes on any
    machine where the vars aren't set, never exercising the autouse fixture's
    ``delenv``. A module-scoped (broader than function) autouse fixture sets them in
    the real environment *before* the per-test ``_isolate_clauster_home`` runs, so
    every test in this module actually has something to strip; ``monkeypatch`` in
    that fixture restores them after each test, so they're present again for the next.
    """
    os.environ["CLAUSTER_CONFIG"] = "/nonexistent/stray-config.yml"
    os.environ["CLAUSTER_STATE_DIR"] = "/nonexistent/stray-state"
    yield
    os.environ.pop("CLAUSTER_CONFIG", None)
    os.environ.pop("CLAUSTER_STATE_DIR", None)


def test_stray_real_env_overrides_are_dropped():
    """CLAUSTER_CONFIG / CLAUSTER_STATE_DIR set in the real env are removed per test.

    They are injected module-wide by ``_inject_stray_overrides`` (and restored after
    each test), so this absence proves the autouse fixture's ``delenv`` actually ran
    rather than passing trivially on a clean environment.
    """
    assert "CLAUSTER_CONFIG" not in os.environ
    assert "CLAUSTER_STATE_DIR" not in os.environ


def test_import_time_home_constants_resolve_off_the_real_home():
    """Module-level ``~``-expanded paths must never point at the developer's real home.

    These freeze at first import (collection time, before any function-scoped fixture),
    so the session-wide HOME pin in conftest is the only thing keeping a test that
    exercises discovery/supervisor from reading or writing the live ``~/.claude.json``
    account. Guard each one so the import-time gap can't silently return.
    """
    from clauster import discovery, pointers, supervisor

    constants = {
        "discovery.CLAUDE_JSON": discovery.CLAUDE_JSON,
        "supervisor.JOBS_DIR": supervisor.JOBS_DIR,
        "supervisor.ROSTER_JSON": supervisor.ROSTER_JSON,
        "pointers.CLAUDE_PROJECTS_DIR": pointers.CLAUDE_PROJECTS_DIR,
    }
    for name, path in constants.items():
        resolved = Path(path).resolve()
        assert resolved != _REAL_HOME, f"{name} resolves to the real home"
        assert _REAL_HOME not in resolved.parents, f"{name} resolves under the real home"
