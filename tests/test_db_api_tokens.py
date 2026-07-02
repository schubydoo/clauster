"""Tests for the named public-API bearer token store + migration (#302).

Covers :class:`~clauster.db.stores.ApiTokenStore` (issue/list/rotate/revoke,
the hot-path ``is_active_hash`` lookup, and the fail-closed / best-effort error
posture mirroring ``StateStore``/``HostedStateStore``) and the ``0004_api_tokens``
migration's reversibility (up creates the table, down drops it and nothing else).
"""

from __future__ import annotations

import logging
from unittest import mock

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from clauster.db import bootstrap
from clauster.db.engine import create_db_engine
from clauster.db.persistence import Persistence

# ----- ApiTokenStore -------------------------------------------------------


@pytest.fixture
def persistence(tmp_path):
    p = Persistence(tmp_path)
    yield p
    p.dispose()


@pytest.fixture
def store(persistence):
    return persistence.api_token_store()


def test_issue_returns_raw_once_and_persists_only_the_hash(store):
    raw, record = store.issue("ci")
    assert raw.startswith("clauster_pat_")
    assert record.label == "ci"
    assert record.last_used_at is None
    assert record.created_at is not None
    # The raw secret never round-trips back out of the store.
    assert not hasattr(record, "token_hash")
    assert raw not in repr(record)


def test_issue_duplicate_label_raises_value_error(store):
    store.issue("ci")
    with pytest.raises(ValueError, match="already exists"):
        store.issue("ci")


def test_issue_raises_oserror_on_db_error(store):
    with mock.patch.object(store, "_sessions", side_effect=SQLAlchemyError("boom")):
        with pytest.raises(OSError, match="api-token issue failed"):
            store.issue("ci")


def test_list_all_empty(store):
    assert store.list_all() == []


def test_list_all_oldest_first(store):
    store.issue("first")
    store.issue("second")
    labels = [r.label for r in store.list_all()]
    assert labels == ["first", "second"]


def test_list_all_degrades_to_empty_on_db_error(store):
    with mock.patch.object(store, "_sessions", side_effect=SQLAlchemyError("boom")):
        assert store.list_all() == []


def test_is_active_hash_true_for_issued_token(store):
    raw, _record = store.issue("ci")
    from clauster.auth import hash_token

    assert store.is_active_hash(hash_token(raw)) is True


def test_is_active_hash_false_for_unknown_hash(store):
    assert store.is_active_hash("0" * 64) is False


def test_is_active_hash_denies_on_db_error(store):
    # Fail-closed: a lookup failure must deny, never authenticate.
    with mock.patch.object(store, "_sessions", side_effect=SQLAlchemyError("boom")):
        assert store.is_active_hash("0" * 64) is False


def test_touch_last_used_updates_the_matching_row(store):
    from clauster.auth import hash_token

    raw, _record = store.issue("ci")
    assert store.list_all()[0].last_used_at is None
    store.touch_last_used(hash_token(raw))
    assert store.list_all()[0].last_used_at is not None


def test_touch_last_used_is_a_noop_for_an_unmatched_hash(store):
    # The legacy config.auth.api_token_hash path has no row — must not raise.
    store.touch_last_used("0" * 64)


def test_touch_last_used_swallows_db_errors(store, caplog):
    # Best-effort: bookkeeping must never fail the request it just authenticated.
    with (
        caplog.at_level(logging.WARNING, logger="clauster.db.stores"),
        mock.patch.object(store, "_sessions", side_effect=SQLAlchemyError("boom")),
    ):
        store.touch_last_used("0" * 64)  # does not raise
    assert any("last-used" in r.message for r in caplog.records)


def test_rotate_changes_the_hash_and_resets_last_used(store):
    from clauster.auth import hash_token

    raw1, _record = store.issue("ci")
    store.touch_last_used(hash_token(raw1))
    assert store.list_all()[0].last_used_at is not None

    raw2, record2 = store.rotate("ci")
    assert raw2 != raw1
    assert record2.label == "ci"

    # The old secret no longer authenticates; the new one does; last_used reset.
    assert store.is_active_hash(hash_token(raw1)) is False
    assert store.is_active_hash(hash_token(raw2)) is True
    assert store.list_all()[0].last_used_at is None


def test_rotate_unknown_label_raises_value_error(store):
    with pytest.raises(ValueError, match="no token labeled"):
        store.rotate("ghost")


def test_rotate_raises_oserror_on_db_error(store):
    store.issue("ci")
    with mock.patch.object(store, "_sessions", side_effect=SQLAlchemyError("boom")):
        with pytest.raises(OSError, match="api-token rotate failed"):
            store.rotate("ci")


def test_revoke_deletes_and_returns_true(store):
    raw, _record = store.issue("ci")
    from clauster.auth import hash_token

    assert store.revoke("ci") is True
    assert store.list_all() == []
    assert store.is_active_hash(hash_token(raw)) is False


def test_revoke_unknown_label_returns_false(store):
    assert store.revoke("ghost") is False


def test_revoke_raises_oserror_on_db_error(store):
    store.issue("ci")
    with mock.patch.object(store, "_sessions", side_effect=SQLAlchemyError("boom")):
        with pytest.raises(OSError, match="api-token revoke failed"):
            store.revoke("ci")


# ----- 0004_api_tokens migration reversibility ------------------------------


def test_api_tokens_migration_up_and_down(tmp_path):
    """The ``api_tokens`` table appears at head and is cleanly dropped on downgrade.

    Downgrading one step (head -> ``b3a1c4f9e021``, the prior revision) must drop
    ONLY ``api_tokens`` — the earlier foundation tables (and their data) survive
    untouched, proving the migration is reversible without collateral damage.
    """
    from alembic import command

    engine = create_db_engine(tmp_path)
    try:
        with engine.connect() as conn:
            cfg = Config(str(bootstrap._ALEMBIC_INI))
            cfg.set_main_option("script_location", str(bootstrap._MIGRATIONS_DIR))
            cfg.attributes["connection"] = conn

            command.upgrade(cfg, "head")
            names = set(
                conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).scalars()
            )
            assert "api_tokens" in names

            conn.execute(
                text(
                    "INSERT INTO api_tokens (label, token_hash, created_at, updated_at) "
                    "VALUES ('ci', :token_hash, '2026-01-01', '2026-01-01')"
                ),
                {"token_hash": "a" * 64},
            )
            assert conn.execute(text("SELECT COUNT(*) FROM api_tokens")).scalar() == 1

            command.downgrade(cfg, "b3a1c4f9e021")
            names = set(
                conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).scalars()
            )
            assert "api_tokens" not in names
            assert {"projects", "instances", "hosted_sessions"} <= names

            command.upgrade(cfg, "head")  # re-upgrade must succeed cleanly (idempotent path)
            names = set(
                conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).scalars()
            )
            assert "api_tokens" in names
            assert conn.execute(text("SELECT COUNT(*) FROM api_tokens")).scalar() == 0
    finally:
        engine.dispose()
