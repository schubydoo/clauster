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
    url = resolve_url(tmp_path)
    assert url.startswith("sqlite:///")
    assert url.endswith(f"/{DB_FILENAME}")


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


IID_A = "aaaaaaaa-0000-0000-0000-000000000001"
IID_B = "bbbbbbbb-0000-0000-0000-000000000002"


def test_state_round_trip_create_read_update(persistence):
    store = persistence.state_store()
    assert store.load() == {}
    store.save({IID_A: {"project_name": "alpha", "label": "A", "spawn_mode": "session"}})
    assert store.load() == {
        IID_A: {"project_name": "alpha", "label": "A", "spawn_mode": "session"}
    }
    # update in place (same key) preserves identity, changes fields
    store.save({IID_A: {"project_name": "alpha", "label": "A2", "permission_mode": "plan"}})
    assert store.load() == {
        IID_A: {"project_name": "alpha", "label": "A2", "permission_mode": "plan"}
    }


def test_state_save_is_full_replace_pruning_absent_keys(persistence):
    store = persistence.state_store()
    store.save(
        {
            IID_A: {"project_name": "a", "label": "a"},
            IID_B: {"project_name": "b", "label": "b"},
        }
    )
    store.save({IID_B: {"project_name": "b", "label": "b2"}})  # a dropped -> pruned
    assert store.load() == {IID_B: {"project_name": "b", "label": "b2"}}


def test_state_absent_fields_stay_absent(persistence):
    store = persistence.state_store()
    store.save({IID_A: {"project_name": "a", "label": "a"}})  # no modes set
    loaded = store.load()[IID_A]
    # None columns omitted, like the JSON store (project_name is always present)
    assert loaded == {"project_name": "a", "label": "a"}


def test_state_update_with_blank_project_name_keeps_existing(persistence):
    # Re-saving an existing instance_id whose record omits project_name must NOT blank
    # the row's project_name (the _sync `if project_name:` guard on the update path).
    store = persistence.state_store()
    store.save({IID_A: {"project_name": "a", "label": "one"}})
    store.save({IID_A: {"label": "two"}})  # no project_name -> keep the prior "a"
    loaded = store.load()[IID_A]
    assert loaded["project_name"] == "a"
    assert loaded["label"] == "two"


def test_state_persists_across_reopen(tmp_path):
    p1 = Persistence(tmp_path)
    p1.state_store().save({IID_A: {"project_name": "a", "label": "kept", "resume_mode": "pty"}})
    p1.dispose()
    p2 = Persistence(tmp_path)
    try:
        assert p2.state_store().load() == {
            IID_A: {"project_name": "a", "label": "kept", "resume_mode": "pty"}
        }
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


def test_hosted_instance_id_round_trips(persistence):
    # Regression guard for #841: the DB-backed hosted store must round-trip
    # instance_id exactly like the JSON store, so a restart can restore the
    # per-runtime id a client may have cached.
    store = persistence.hosted_state_store()
    iid = "33333333-3333-4333-8333-333333333333"
    store.save({"pid-1": {"project": "alpha", "instance_id": iid}})
    assert store.load() == {"pid-1": {"project": "alpha", "instance_id": iid}}


def test_hosted_instance_id_absent_stays_absent(persistence):
    # A record saved without instance_id (older client, or a pre-migration row)
    # loads with the column NULL -> omitted, same "absent stays absent" contract
    # as every other nullable hosted field. The caller's `.get("instance_id")`
    # then falls through to the model's default_factory, minting a fresh id.
    store = persistence.hosted_state_store()
    store.save({"pid-1": {"project": "alpha"}})
    loaded = store.load()["pid-1"]
    assert "instance_id" not in loaded


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
            store.save({IID_A: {"project_name": "a", "label": "a"}})


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
                upgrade_to_head(engine, tmp_path)
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


# ----- pre-migration snapshot (#795) -------------------------------------


def test_snapshot_written_when_migration_pending(tmp_path):
    # Migrate only through 0002 (0003 left pending), then bring to head via
    # upgrade_to_head: since current != head, a pre-migration snapshot must land
    # in state_dir/backups/ before Alembic runs.
    from alembic import command as alembic_command

    engine = create_db_engine(tmp_path)
    try:
        with engine.connect() as conn:
            cfg = Config(str(bootstrap._ALEMBIC_INI))
            cfg.set_main_option("script_location", str(bootstrap._MIGRATIONS_DIR))
            cfg.attributes["connection"] = conn
            alembic_command.upgrade(cfg, "f4424422f656")  # stop just before 0003
        backups_dir = tmp_path / "backups"
        assert not backups_dir.exists()
        upgrade_to_head(engine, tmp_path)
        snapshots = list(backups_dir.glob("pre-*.db"))
        assert len(snapshots) == 1
        assert snapshots[0].name.startswith("pre-f4424422f656-")
    finally:
        engine.dispose()


def test_snapshot_not_written_on_already_at_head_restart(tmp_path):
    # A fresh DB's very first migration IS pending (current=None != head), so the
    # first upgrade_to_head writes one snapshot. A second call against the same,
    # now-current schema must NOT write another — the plain-restart no-op case.
    engine = create_db_engine(tmp_path)
    try:
        upgrade_to_head(engine, tmp_path)
        backups_dir = tmp_path / "backups"
        first = list(backups_dir.glob("pre-*.db"))
        assert len(first) == 1
        upgrade_to_head(engine, tmp_path)
        assert list(backups_dir.glob("pre-*.db")) == first
    finally:
        engine.dispose()


def test_backup_before_migrate_false_skips_snapshot_entirely(tmp_path):
    engine = create_db_engine(tmp_path)
    try:
        upgrade_to_head(engine, tmp_path, backup_before_migrate=False)
        assert not (tmp_path / "backups").exists()
    finally:
        engine.dispose()


def test_prune_snapshots_keeps_only_the_newest_n_by_mtime(tmp_path):
    # Retention must key on mtime, NOT filename: the name is pre-<current>-<head>-<stamp>
    # and the leading revision ids are arbitrary Alembic hashes. Here the revision-id
    # prefixes are deliberately ordered OPPOSITE to time (the newest file has the
    # lexicographically-smallest prefix), so a name-sort would wrongly prune the newest.
    import os

    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    # index 0 = oldest (prefix "zzz"), index 7 = newest (prefix "sss") — prefix order is
    # the REVERSE of time order, so a lexicographic sort disagrees with mtime.
    prefixes = ["zzz", "yyy", "xxx", "www", "vvv", "uuu", "ttt", "sss"]
    made = []
    for i, pfx in enumerate(prefixes):
        p = backups_dir / f"pre-{pfx}-head-2026010{i}T000000_000000Z.db"
        p.write_text("x")
        os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))  # ascending mtime = increasing recency
        made.append(p)
    bootstrap._prune_snapshots(backups_dir, keep=5)
    remaining = set(backups_dir.glob("pre-*.db"))
    assert remaining == set(made[-5:])  # the 5 newest-by-mtime survive
    # Regression guard vs a name-sort: the newest file (smallest-sorting prefix) must live,
    # the oldest (largest-sorting prefix) must be pruned.
    assert made[-1] in remaining
    assert made[0] not in remaining


def test_snapshot_prunes_pre_existing_snapshots_beyond_retention(tmp_path):
    # 6 pre-existing snapshot files + 1 freshly written one must prune to the
    # default retention of 5 — exercising the prune call inside the snapshot path.
    engine = create_db_engine(tmp_path)
    try:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        for i in range(6):
            (backups_dir / f"pre-old-old-2026010{i}T000000_000000Z.db").write_text("x")
        with engine.connect() as conn:
            current, head = bootstrap._pending_revision(conn)
        bootstrap._snapshot_before_migrate(engine, tmp_path, current, head)
        assert len(list(backups_dir.glob("pre-*.db"))) == 5
    finally:
        engine.dispose()


def test_snapshot_failure_logs_warning_and_migration_still_proceeds(tmp_path, caplog):
    # Failure policy (#795): a snapshot write failure is a WARNING, never fatal —
    # the migration (already transactional/fail-closed) must still complete.
    engine = create_db_engine(tmp_path)
    try:
        with (
            caplog.at_level(logging.WARNING, logger="clauster.db.bootstrap"),
            mock.patch("sqlite3.connect", side_effect=OSError("disk full")),
        ):
            upgrade_to_head(engine, tmp_path)  # must not raise
        assert "pre-migration snapshot failed" in caplog.text
        with engine.connect() as conn:
            current, head = bootstrap._pending_revision(conn)
        assert current == head  # migration completed despite the snapshot failure
    finally:
        engine.dispose()


def test_snapshot_skips_in_memory_engine(tmp_path):
    engine = create_engine("sqlite://", future=True, connect_args={"check_same_thread": False})
    try:
        bootstrap._snapshot_before_migrate(engine, tmp_path, "a", "b")
        assert not (tmp_path / "backups").exists()
    finally:
        engine.dispose()


def test_snapshot_skips_when_db_file_not_yet_created(tmp_path):
    # The engine is built but never connected, so sqlite hasn't created the file
    # yet — nothing to snapshot.
    db_path = tmp_path / "clauster.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    try:
        assert not db_path.exists()
        bootstrap._snapshot_before_migrate(engine, tmp_path, "a", "b")
        assert not (tmp_path / "backups").exists()
    finally:
        engine.dispose()


def test_persistence_snapshots_on_first_boot_by_default(tmp_path):
    p = Persistence(tmp_path)
    try:
        assert list((tmp_path / "backups").glob("pre-*.db"))
    finally:
        p.dispose()


def test_persistence_backup_before_migrate_false_skips_snapshot(tmp_path):
    p = Persistence(tmp_path, backup_before_migrate=False)
    try:
        assert not (tmp_path / "backups").exists()
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


def _legacy_iid(project_name):
    # Mirrors bootstrap._project_instance_id: the deterministic UUID the import
    # derives for a legacy project-keyed record (issue 777).
    import uuid as _uuid

    return str(_uuid.uuid5(_uuid.NAMESPACE_DNS, f"clauster.instance.{project_name}"))


def test_first_boot_imports_legacy_json_and_retires_files(tmp_path):
    _seed_json(tmp_path)
    p = Persistence(tmp_path)
    try:
        assert p.state_store().load() == {
            _legacy_iid("alpha"): {
                "project_name": "alpha",
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
        projects = {v.get("project_name") for v in loaded.values()}
        assert "ghost" not in projects  # not re-imported over existing rows
        assert "alpha" in projects
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
        assert p.state_store().load() == {
            _legacy_iid("solo"): {"project_name": "solo", "label": "S"}
        }
        assert p.hosted_state_store().load() == {}
    finally:
        p.dispose()
    assert (tmp_path / "state.json.imported").exists()
    assert not (tmp_path / "hosted_state.json.imported").exists()


def test_import_failure_leaves_json_intact(tmp_path):
    _seed_json(tmp_path)
    engine = create_db_engine(tmp_path)
    upgrade_to_head(engine, tmp_path)
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
    upgrade_to_head(engine, tmp_path)
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
    upgrade_to_head(engine, tmp_path)
    factory = make_session_factory(engine)
    try:
        assert import_legacy_json(tmp_path, factory) is False
    finally:
        engine.dispose()
    assert (tmp_path / "state.json.imported").exists()
    assert (tmp_path / "hosted_state.json.imported").exists()
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "hosted_state.json").exists()


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
    # Connect directly at db_path (not create_db_engine, which always targets
    # <state_dir>/clauster.db) — this only verifies the migration wrote the tables.
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
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
    # Migration 0003's offline branch: the instance-id re-key emits its set-based
    # copy into instances_new (no per-row Python INSERT, since there's no connection
    # to read rows through). Asserting it proves the is_offline_mode() branch ran.
    assert "INSERT INTO instances_new" in out
    assert "randomblob" in out  # the offline-only random-UUID id expression


def test_instance_id_migration_rekeys_existing_row_with_deterministic_uuid(tmp_path):
    # Online upgrade THROUGH 0003 with a pre-existing (project-name-keyed) instances
    # row: the re-key copies it to the new instance_id PK using the deterministic
    # UUID5 (namespace=DNS, name="clauster.instance.<project>"). Covers 0003's online
    # copy loop + _project_instance_id — a fresh-empty-DB upgrade has no rows to copy.
    import uuid

    from alembic import command

    expected_iid = str(uuid.uuid5(uuid.NAMESPACE_DNS, "clauster.instance.alpha"))
    engine = create_db_engine(tmp_path)
    try:
        with engine.connect() as conn:
            cfg = Config(str(bootstrap._ALEMBIC_INI))
            cfg.set_main_option("script_location", str(bootstrap._MIGRATIONS_DIR))
            cfg.attributes["connection"] = conn
            # Stop just before 0003 (0002 is the session_events revision), then seed a
            # legacy row keyed by project_name (the pre-777 shape).
            command.upgrade(cfg, "f4424422f656")
            conn.execute(
                text(
                    "INSERT INTO projects (name, created_at, updated_at) "
                    "VALUES ('alpha', '2026-01-01', '2026-01-01')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO instances "
                    "(project_name, label, resume_mode, created_at, updated_at) "
                    "VALUES ('alpha', 'Alpha', 'pty', '2026-01-01', '2026-01-01')"
                )
            )
            command.upgrade(cfg, "head")  # runs 0003's re-key over the seeded row
            rows = conn.execute(
                text("SELECT instance_id, project_name, label, resume_mode FROM instances")
            ).all()
        assert rows == [(expected_iid, "alpha", "Alpha", "pty")]
    finally:
        engine.dispose()


def test_hosted_instance_id_migration_adds_and_drops_nullable_column(tmp_path):
    # 0005 adds a nullable instance_id column to hosted_sessions (#841); upgrading
    # to head must expose it, and downgrading one step must cleanly remove it
    # again without disturbing the rest of the table (SQLite has no native ALTER,
    # so this also covers the batch_alter_table add/drop path on this backend).
    from alembic import command

    engine = create_db_engine(tmp_path)
    try:
        with engine.connect() as conn:
            cfg = Config(str(bootstrap._ALEMBIC_INI))
            cfg.set_main_option("script_location", str(bootstrap._MIGRATIONS_DIR))
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            columns = {
                row[1] for row in conn.execute(text("PRAGMA table_info(hosted_sessions)")).all()
            }
            assert "instance_id" in columns

            command.downgrade(cfg, "c28a9ef64664")  # one step back, pre-0005
            columns = {
                row[1] for row in conn.execute(text("PRAGMA table_info(hosted_sessions)")).all()
            }
            assert "instance_id" not in columns
            assert "claustrum_process_id" in columns  # the rest of the table survives
    finally:
        engine.dispose()


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
