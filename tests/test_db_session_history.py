"""Tests for the session lifecycle / event-history layer (#363).

Covers the ``session_events`` migration (upgrade/downgrade chain on top of the
foundation baseline), the FK→projects cascade, and the
:class:`~clauster.db.stores.SessionHistoryStore` API: append, per-project + global
history, the "last used / total cost" rollup math (with and without a terminal
row), the missing-project edge case, the ``limit`` cap, and the fail-closed
read / best-effort write posture on a DB error.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from clauster.db import bootstrap
from clauster.db.engine import create_db_engine
from clauster.db.persistence import Persistence
from clauster.db.stores import HistoryEvent, ProjectRollup, SessionHistoryStore


@pytest.fixture
def persistence(tmp_path):
    p = Persistence(tmp_path)
    yield p
    p.dispose()


@pytest.fixture
def store(persistence) -> SessionHistoryStore:
    return persistence.session_history_store()


# ----- migration: table + indexes + downgrade chain ----------------------


def test_migration_creates_session_events_table_and_indexes(persistence):
    with persistence.session_factory() as session:
        tables = set(
            session.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).scalars()
        )
        indexes = set(
            session.execute(text("SELECT name FROM sqlite_master WHERE type='index'")).scalars()
        )
    assert "session_events" in tables
    assert {"ix_session_events_project_at", "ix_session_events_at"} <= indexes


def test_session_events_migration_downgrade_one_step_leaves_foundation(tmp_path):
    # Upgrade to head, then downgrade a single step: session_events is dropped but the
    # foundation tables (projects/instances/hosted_sessions) remain. Covers the new
    # migration's downgrade() against an injected connection.
    engine = create_db_engine(tmp_path)
    try:
        with engine.connect() as conn:
            cfg = Config(str(bootstrap._ALEMBIC_INI))
            cfg.set_main_option("script_location", str(bootstrap._MIGRATIONS_DIR))
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            command.downgrade(cfg, "-1")
            names = set(
                conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).scalars()
            )
        assert "session_events" not in names
        assert {"projects", "instances", "hosted_sessions"} <= names
    finally:
        engine.dispose()


def test_session_events_full_downgrade_drops_everything(tmp_path):
    engine = create_db_engine(tmp_path)
    try:
        with engine.connect() as conn:
            cfg = Config(str(bootstrap._ALEMBIC_INI))
            cfg.set_main_option("script_location", str(bootstrap._MIGRATIONS_DIR))
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            command.downgrade(cfg, "base")
            names = set(
                conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).scalars()
            )
        assert not ({"projects", "instances", "hosted_sessions", "session_events"} & names)
    finally:
        engine.dispose()


# ----- append + auto-created parent project ------------------------------


def test_append_autocreates_parent_project(persistence, store):
    # A first event for an unseen project creates the FK parent (mirrors StateStore).
    assert store.append(project_name="alpha", mode="pty", kind="spawned") is True
    with persistence.session_factory() as session:
        projects = set(session.execute(text("SELECT name FROM projects")).scalars())
    assert "alpha" in projects


def test_append_returns_true_on_success(store):
    assert store.append(project_name="alpha", mode="standard", kind="ready") is True


# ----- per-project + global history round-trip ---------------------------


def test_history_for_returns_project_events_newest_first(store):
    base = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
    store.append(project_name="alpha", mode="pty", kind="spawned", at=base)
    store.append(project_name="alpha", mode="pty", kind="ready", at=base + timedelta(minutes=1))
    store.append(project_name="alpha", mode="pty", kind="ended", at=base + timedelta(minutes=2))
    store.append(project_name="beta", mode="standard", kind="spawned", at=base)

    events = store.history_for("alpha")
    assert [e.kind for e in events] == ["ended", "ready", "spawned"]
    assert all(isinstance(e, HistoryEvent) for e in events)
    assert all(e.project_name == "alpha" for e in events)


def test_history_global_spans_all_projects_newest_first(store):
    base = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
    store.append(project_name="alpha", mode="pty", kind="spawned", at=base)
    store.append(
        project_name="beta", mode="standard", kind="spawned", at=base + timedelta(hours=1)
    )
    events = store.history()
    assert len(events) == 2
    # Newest first: beta's later spawn precedes alpha's.
    assert [e.project_name for e in events] == ["beta", "alpha"]


def test_history_limit_caps_rows(store):
    base = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
    for i in range(5):
        store.append(
            project_name="alpha", mode="pty", kind="spawned", at=base + timedelta(minutes=i)
        )
    assert len(store.history_for("alpha", limit=2)) == 2
    assert len(store.history(limit=3)) == 3


def test_history_for_unknown_project_is_empty(store):
    store.append(project_name="alpha", mode="pty", kind="spawned")
    assert store.history_for("ghost") == []


def test_terminal_row_carries_cost_snapshot_nonterminal_does_not(store):
    store.append(project_name="alpha", mode="pty", kind="spawned")
    store.append(
        project_name="alpha",
        mode="pty",
        kind="ended",
        cost_usd=4.2,
        input_tokens=100,
        output_tokens=50,
        cache_creation_tokens=10,
        cache_read_tokens=5,
    )
    events = {e.kind: e for e in store.history_for("alpha")}
    assert events["spawned"].cost_usd is None
    assert events["spawned"].input_tokens is None
    assert events["ended"].cost_usd == 4.2
    assert events["ended"].input_tokens == 100
    assert events["ended"].cache_read_tokens == 5


def test_append_defaults_at_to_now_when_omitted(store):
    before = datetime.now(tz=UTC)
    store.append(project_name="alpha", mode="pty", kind="spawned")
    after = datetime.now(tz=UTC)
    [event] = store.history_for("alpha")
    # SQLite returns a naive datetime for a stored tz-aware value; compare as UTC.
    at = event.at if event.at.tzinfo else event.at.replace(tzinfo=UTC)
    assert before <= at <= after


# ----- rollup: last used / total cost ------------------------------------


def test_rollup_uses_latest_terminal_row_cost(store):
    base = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
    store.append(project_name="alpha", mode="pty", kind="spawned", at=base)
    store.append(
        project_name="alpha",
        mode="pty",
        kind="ended",
        at=base + timedelta(hours=1),
        cost_usd=1.0,
        input_tokens=10,
    )
    # A second, later session ends with a larger cumulative snapshot — the rollup must
    # reflect the most recent terminal row, not the first.
    store.append(project_name="alpha", mode="pty", kind="spawned", at=base + timedelta(hours=2))
    store.append(
        project_name="alpha",
        mode="pty",
        kind="ended",
        at=base + timedelta(hours=3),
        cost_usd=3.5,
        input_tokens=42,
    )
    rollup = store.rollup_for("alpha")
    assert isinstance(rollup, ProjectRollup)
    assert rollup.total_cost_usd == 3.5
    assert rollup.input_tokens == 42
    assert rollup.event_count == 4
    last_used = (
        rollup.last_used.replace(tzinfo=UTC)
        if rollup.last_used.tzinfo is None
        else rollup.last_used
    )
    assert last_used == base + timedelta(hours=3)


def test_rollup_crash_counts_as_terminal(store):
    store.append(project_name="alpha", mode="standard", kind="spawned")
    store.append(project_name="alpha", mode="standard", kind="crashed", cost_usd=0.75)
    rollup = store.rollup_for("alpha")
    assert rollup.total_cost_usd == 0.75


def test_rollup_with_no_terminal_row_has_null_cost(store):
    store.append(project_name="alpha", mode="pty", kind="spawned")
    store.append(project_name="alpha", mode="pty", kind="ready")
    rollup = store.rollup_for("alpha")
    assert rollup.last_used is not None
    assert rollup.event_count == 2
    assert rollup.total_cost_usd is None
    assert rollup.input_tokens is None


def test_rollup_for_unknown_project_is_empty(store):
    rollup = store.rollup_for("ghost")
    assert rollup == ProjectRollup(project_name="ghost")
    assert rollup.last_used is None
    assert rollup.event_count == 0
    assert rollup.total_cost_usd is None


# ----- FK cascade ---------------------------------------------------------


def test_deleting_project_cascades_to_session_events(persistence, store):
    store.append(project_name="alpha", mode="pty", kind="spawned")
    store.append(project_name="alpha", mode="pty", kind="ended", cost_usd=1.0)
    with persistence.session_factory() as session, session.begin():
        session.execute(text("DELETE FROM projects WHERE name = 'alpha'"))
    assert store.history_for("alpha") == []


# ----- persistence across reopen -----------------------------------------


def test_history_persists_across_reopen(tmp_path):
    p1 = Persistence(tmp_path)
    p1.session_history_store().append(project_name="alpha", mode="pty", kind="ended", cost_usd=2.0)
    p1.dispose()
    p2 = Persistence(tmp_path)
    try:
        rollup = p2.session_history_store().rollup_for("alpha")
        assert rollup.total_cost_usd == 2.0
        assert rollup.event_count == 1
    finally:
        p2.dispose()


# ----- fail-closed read + best-effort write ------------------------------


def test_append_returns_false_on_db_error(store):
    # A write failure is swallowed (best-effort): history is non-authoritative, so a
    # lost row must never raise into the bridge lifecycle.
    with mock.patch.object(store, "_sessions", side_effect=SQLAlchemyError("boom")):
        assert store.append(project_name="alpha", mode="pty", kind="spawned") is False


def test_history_for_degrades_to_empty_on_db_error(store):
    with mock.patch.object(store, "_sessions", side_effect=SQLAlchemyError("boom")):
        assert store.history_for("alpha") == []


def test_history_degrades_to_empty_on_db_error(store):
    with mock.patch.object(store, "_sessions", side_effect=SQLAlchemyError("boom")):
        assert store.history() == []


def test_rollup_degrades_to_empty_on_db_error(store):
    with mock.patch.object(store, "_sessions", side_effect=SQLAlchemyError("boom")):
        rollup = store.rollup_for("alpha")
        assert rollup == ProjectRollup(project_name="alpha")
