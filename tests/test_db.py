"""Tests for the persistence foundation (#362): engine, stores, bootstrap, import.

Covers the round-trip the issue's "Done when" calls for (create/read/update),
the one-time JSON→SQLite import, the fail-closed migration + import paths, the
SQLite PRAGMAs, and the full-replace prune semantics the JSON callers rely on.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from clauster.db import bootstrap
from clauster.db.bootstrap import MigrationError, import_legacy_json, upgrade_to_head
from clauster.db.engine import (
    DB_FILENAME,
    create_db_engine,
    make_session_factory,
    resolve_url,
)
from clauster.db.persistence import Persistence

# ----- engine ------------------------------------------------------------


def test_resolve_url_defaults_to_sqlite_under_state_dir(tmp_path):
    url = resolve_url(tmp_path, None)
    assert url.startswith("sqlite:///")
    assert url.endswith(f"/{DB_FILENAME}")


def test_resolve_url_honors_explicit_database_url(tmp_path):
    assert resolve_url(tmp_path, "postgresql+psycopg://x/y") == "postgresql+psycopg://x/y"


def test_sqlite_pragmas_are_armed(tmp_path):
    engine = create_db_engine(tmp_path)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
            assert conn.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"
            assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000
    finally:
        engine.dispose()


def test_create_engine_creates_state_dir_0700(tmp_path):
    state_dir = tmp_path / "nested" / "state"
    engine = create_db_engine(state_dir)
    try:
        assert state_dir.is_dir()
    finally:
        engine.dispose()


# ----- StateStore round-trip --------------------------------------------


@pytest.fixture
def persistence(tmp_path):
    p = Persistence(tmp_path)
    yield p
    p.dispose()


def test_state_round_trip_create_read_update(persistence):
    store = persistence.state_store()
    assert store.load() == {}
    store.save({"alpha": {"label": "A", "spawn_mode": "session"}})
    assert store.load() == {"alpha": {"label": "A", "spawn_mode": "session"}}
    # update in place (same key) preserves identity, changes fields
    store.save({"alpha": {"label": "A2", "permission_mode": "plan"}})
    assert store.load() == {"alpha": {"label": "A2", "permission_mode": "plan"}}


def test_state_save_is_full_replace_pruning_absent_keys(persistence):
    store = persistence.state_store()
    store.save({"a": {"label": "a"}, "b": {"label": "b"}})
    store.save({"b": {"label": "b2"}})  # a dropped -> pruned
    assert store.load() == {"b": {"label": "b2"}}


def test_state_absent_fields_stay_absent(persistence):
    store = persistence.state_store()
    store.save({"a": {"label": "a"}})  # no modes set
    loaded = store.load()["a"]
    assert loaded == {"label": "a"}  # None columns omitted, like the JSON store


def test_state_persists_across_reopen(tmp_path):
    p1 = Persistence(tmp_path)
    p1.state_store().save({"a": {"label": "kept", "resume_mode": "pty"}})
    p1.dispose()
    p2 = Persistence(tmp_path)
    try:
        assert p2.state_store().load() == {"a": {"label": "kept", "resume_mode": "pty"}}
    finally:
        p2.dispose()


# ----- HostedStateStore round-trip --------------------------------------


def test_hosted_round_trip(persistence):
    store = persistence.hosted_state_store()
    store.save(
        {"pid-1": {"project": "alpha", "label": "H", "daemon_last_seq": 9, "agent_pid": 42}}
    )
    assert store.load() == {
        "pid-1": {"project": "alpha", "label": "H", "daemon_last_seq": 9, "agent_pid": 42}
    }


def test_hosted_save_prunes_absent_keys(persistence):
    store = persistence.hosted_state_store()
    store.save({"pid-1": {"project": "a"}, "pid-2": {"project": "b"}})
    store.save({"pid-2": {"project": "b2"}})
    assert store.load() == {"pid-2": {"project": "b2"}}


# ----- fail-closed read + raising save -----------------------------------


def test_state_load_degrades_to_empty_on_db_error(persistence):
    store = persistence.state_store()
    # A read failure must degrade to {} (the JSON store's corrupt-file fail-closed
    # posture), never crash on startup.
    with mock.patch.object(store, "_sessions", side_effect=SQLAlchemyError("boom")):
        assert store.load() == {}


def test_hosted_load_degrades_to_empty_on_db_error(persistence):
    store = persistence.hosted_state_store()
    with mock.patch.object(store, "_sessions", side_effect=SQLAlchemyError("boom")):
        assert store.load() == {}


def test_state_save_raises_oserror_on_db_error(persistence):
    store = persistence.state_store()
    # A write failure surfaces as OSError so the callers' best-effort `except OSError`
    # (a stale cursor, never a failed spawn) keeps working unchanged.
    with mock.patch.object(store, "_sessions", side_effect=SQLAlchemyError("boom")):
        with pytest.raises(OSError, match="state save failed"):
            store.save({"a": {"label": "a"}})


def test_hosted_save_raises_oserror_on_db_error(persistence):
    store = persistence.hosted_state_store()
    with mock.patch.object(store, "_sessions", side_effect=SQLAlchemyError("boom")):
        with pytest.raises(OSError, match="hosted-state save failed"):
            store.save({"pid-1": {"project": "a"}})


# ----- migration fail-closed --------------------------------------------


def test_upgrade_to_head_wraps_failure_in_migration_error(tmp_path):
    engine = create_db_engine(tmp_path)
    try:
        with mock.patch.object(bootstrap.command, "upgrade", side_effect=RuntimeError("boom")):
            with pytest.raises(MigrationError, match="boom"):
                upgrade_to_head(engine)
    finally:
        engine.dispose()


def test_persistence_propagates_migration_error(tmp_path):
    with mock.patch.object(bootstrap.command, "upgrade", side_effect=RuntimeError("boom")):
        with pytest.raises(MigrationError):
            Persistence(tmp_path)


def test_migration_creates_all_foundation_tables(tmp_path):
    p = Persistence(tmp_path)
    try:
        with p.session_factory() as session:
            names = set(
                session.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                ).scalars()
            )
        assert {"projects", "instances", "hosted_sessions"} <= names
    finally:
        p.dispose()


# ----- one-time JSON import ----------------------------------------------


def _seed_json(state_dir):
    (state_dir / "state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "instances": {
                    "alpha": {
                        "label": "A",
                        "intentional_stop": True,
                        "spawn_mode": "session",
                        "permission_mode": "plan",
                    }
                },
            }
        )
    )
    (state_dir / "hosted_state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sessions": {"pid-1": {"project": "alpha", "label": "H", "daemon_last_seq": 5}},
            }
        )
    )


def test_first_boot_imports_legacy_json_and_retires_files(tmp_path):
    _seed_json(tmp_path)
    p = Persistence(tmp_path)
    try:
        assert p.state_store().load() == {
            "alpha": {
                "label": "A",
                "intentional_stop": True,
                "spawn_mode": "session",
                "permission_mode": "plan",
            }
        }
        assert p.hosted_state_store().load() == {
            "pid-1": {"project": "alpha", "label": "H", "daemon_last_seq": 5}
        }
    finally:
        p.dispose()
    # The JSON files are retired (renamed), not deleted.
    assert not (tmp_path / "state.json").exists()
    assert (tmp_path / "state.json.imported").exists()
    assert not (tmp_path / "hosted_state.json").exists()
    assert (tmp_path / "hosted_state.json.imported").exists()


def test_import_is_one_time_not_repeated_on_reopen(tmp_path):
    _seed_json(tmp_path)
    Persistence(tmp_path).dispose()  # first boot imports + retires
    # Recreate a stray state.json (as if a user copied an old file back); the
    # schema is no longer empty, so it must NOT be re-imported on top of live rows.
    (tmp_path / "state.json").write_text(
        json.dumps({"schema_version": 1, "instances": {"ghost": {"label": "G"}}})
    )
    p = Persistence(tmp_path)
    try:
        loaded = p.state_store().load()
        assert "ghost" not in loaded  # not re-imported over existing rows
        assert "alpha" in loaded
    finally:
        p.dispose()
    assert (tmp_path / "state.json").exists()  # left intact (no re-import)


def test_no_import_when_no_legacy_json(tmp_path):
    p = Persistence(tmp_path)
    try:
        assert p.state_store().load() == {}
    finally:
        p.dispose()


def test_import_with_only_state_json_present(tmp_path):
    # Only state.json exists (no hosted_state.json): it imports + retires, and the
    # absent hosted file's _retire is a no-op (never errors on a missing path).
    (tmp_path / "state.json").write_text(
        json.dumps({"schema_version": 1, "instances": {"solo": {"label": "S"}}})
    )
    p = Persistence(tmp_path)
    try:
        assert p.state_store().load() == {"solo": {"label": "S"}}
        assert p.hosted_state_store().load() == {}
    finally:
        p.dispose()
    assert (tmp_path / "state.json.imported").exists()
    assert not (tmp_path / "hosted_state.json.imported").exists()


def test_import_failure_leaves_json_intact(tmp_path):
    _seed_json(tmp_path)
    engine = create_db_engine(tmp_path)
    upgrade_to_head(engine)
    factory = make_session_factory(engine)
    try:
        with mock.patch.object(bootstrap.StateStore, "_sync", side_effect=SQLAlchemyError):
            assert import_legacy_json(tmp_path, factory) is False
    finally:
        engine.dispose()
    # Fail-closed: the JSON is untouched, available for a retry next boot.
    assert (tmp_path / "state.json").exists()
    assert not (tmp_path / "state.json.imported").exists()


def test_retire_tolerates_rename_failure(tmp_path, caplog):
    _seed_json(tmp_path)
    engine = create_db_engine(tmp_path)
    upgrade_to_head(engine)
    factory = make_session_factory(engine)
    try:
        with mock.patch("pathlib.Path.rename", side_effect=OSError("denied")):
            # Import still succeeds; the rename failure is logged, not fatal.
            assert import_legacy_json(tmp_path, factory) is True
    finally:
        engine.dispose()
    assert "could not retire" in caplog.text
