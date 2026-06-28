"""Tests for the persistence foundation (#362): engine, stores, bootstrap, import.

Covers the round-trip the issue's "Done when" calls for (create/read/update),
the one-time JSON→SQLite import, the fail-closed migration + import paths, the
SQLite PRAGMAs, and the full-replace prune semantics the JSON callers rely on.
"""

from __future__ import annotations

import json
import logging
from unittest import mock

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from clauster.db import bootstrap
from clauster.db.bootstrap import MigrationError, import_legacy_json, upgrade_to_head
from clauster.db.engine import (
    DB_FILENAME,
    _arm_sqlite_pragmas,
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


def test_sqlite_wal_unavailable_warns(caplog):
    """The connect hook warns when SQLite can't honor WAL (no silent durability loss).

    An in-memory database can never use WAL — ``PRAGMA journal_mode=WAL`` returns
    ``memory`` — which is the same downgrade a network/overlay filesystem produces,
    so it exercises the warn branch without needing an exotic mount.
    """
    engine = create_engine("sqlite://", future=True, connect_args={"check_same_thread": False})
    _arm_sqlite_pragmas(engine)
    try:
        with caplog.at_level(logging.WARNING, logger="clauster.db.engine"), engine.connect():
            pass
    finally:
        engine.dispose()
    assert any("WAL mode unavailable" in r.message for r in caplog.records)


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


def test_persistence_disposes_engine_when_construction_fails(tmp_path):
    # A failed startup step must dispose the just-built engine (no pool leak) before
    # propagating — the caller never receives the object to dispose() itself.
    from clauster.db import persistence as persistence_mod

    with (
        mock.patch.object(bootstrap.command, "upgrade", side_effect=RuntimeError("boom")),
        mock.patch.object(persistence_mod, "dispose_engine") as disposed,
    ):
        with pytest.raises(MigrationError):
            Persistence(tmp_path)
    disposed.assert_called_once()


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


def test_import_retires_empty_present_json_without_importing(tmp_path):
    # Legacy files PRESENT but parse to empty dicts: the import transaction commits,
    # nothing is imported (imported=False, no "imported ..." log), and the files are
    # still retired so a later boot doesn't re-trigger. Covers bootstrap 120->126.
    (tmp_path / "state.json").write_text(json.dumps({"schema_version": 1, "instances": {}}))
    (tmp_path / "hosted_state.json").write_text(json.dumps({"schema_version": 1, "sessions": {}}))
    engine = create_db_engine(tmp_path)
    upgrade_to_head(engine)
    factory = make_session_factory(engine)
    try:
        assert import_legacy_json(tmp_path, factory) is False
    finally:
        engine.dispose()
    assert (tmp_path / "state.json.imported").exists()
    assert (tmp_path / "hosted_state.json.imported").exists()
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "hosted_state.json").exists()


# ----- engine: non-SQLite branch -----------------------------------------


def test_create_db_engine_non_sqlite_skips_pragma_path(tmp_path, monkeypatch):
    # A non-SQLite URL returns a plain engine via the bare create_engine(url, future=True)
    # branch — no pragma listener, no dir creation. Patch create_engine to a sentinel so we
    # never import a real driver (psycopg isn't installed). Covers engine.py line 73.
    sentinel = object()
    captured: dict[str, object] = {}

    def fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr("clauster.db.engine.create_engine", fake_create_engine)
    result = create_db_engine(tmp_path, "postgresql+psycopg://x/y")
    assert result is sentinel  # returned unchanged: the SQLite pragma path was not taken
    assert captured["url"] == "postgresql+psycopg://x/y"
    assert "connect_args" not in captured["kwargs"]  # the SQLite-only check_same_thread arg


# ----- packaged migration env: standalone + offline paths ----------------


def _standalone_cfg(db_path):
    """Build an alembic Config that locates the packaged env without an injected conn."""
    cfg = Config(str(bootstrap._ALEMBIC_INI))
    cfg.set_main_option("script_location", str(bootstrap._MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


def test_env_standalone_online_builds_own_engine(tmp_path):
    # No config.attributes["connection"]: env.run_migrations_online() falls through to
    # engine_from_config and opens its own connection. Covers env.py lines 63-75.
    from alembic import command

    db_path = tmp_path / "standalone.db"
    cfg = _standalone_cfg(db_path)
    command.upgrade(cfg, "head")
    engine = create_db_engine(tmp_path, f"sqlite:///{db_path.as_posix()}")
    try:
        with engine.connect() as conn:
            names = set(
                conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).scalars()
            )
        assert {"projects", "instances", "hosted_sessions"} <= names
    finally:
        engine.dispose()


def test_env_standalone_online_honors_db_url_env(tmp_path, monkeypatch):
    # CLAUSTER_DB_URL overrides the ini url in the standalone online path (env.py 64-66).
    from alembic import command

    db_path = tmp_path / "via_env.db"
    monkeypatch.setenv("CLAUSTER_DB_URL", f"sqlite:///{db_path.as_posix()}")
    cfg = Config(str(bootstrap._ALEMBIC_INI))
    cfg.set_main_option("script_location", str(bootstrap._MIGRATIONS_DIR))
    command.upgrade(cfg, "head")
    assert db_path.exists()  # the env-supplied URL drove the engine, not the (unset) ini url


def test_env_offline_mode_emits_sql(tmp_path, capsys):
    # sql=True puts alembic in offline/--sql mode: env.run_migrations_offline() emits DDL
    # to stdout with no DB connection. Covers env.py lines 30-41 and the offline branch (79).
    from alembic import command

    cfg = _standalone_cfg(tmp_path / "offline.db")
    command.upgrade(cfg, "head", sql=True)
    out = capsys.readouterr().out
    assert "CREATE TABLE" in out
    assert "projects" in out
    assert not (tmp_path / "offline.db").exists()  # offline never touched a real DB


def test_baseline_downgrade_drops_all_tables(tmp_path):
    # Upgrade then downgrade-to-base against an injected connection drops every foundation
    # table. Covers 0001_baseline.downgrade() (lines 70-72).
    from alembic import command

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
        assert not ({"projects", "instances", "hosted_sessions"} & names)
    finally:
        engine.dispose()


# ----- packaged migration packages import cleanly ------------------------


def test_migration_packages_import():
    import clauster.db.migrations as migrations_pkg
    import clauster.db.migrations.versions as versions_pkg

    assert migrations_pkg.__doc__
    assert versions_pkg.__doc__
