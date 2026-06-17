"""Tests for the hosted-session persistence store (CL-6).

A small sibling of ``test_state``: round-trip, tolerant load of a missing/corrupt
file, schema migration with a one-time backup, and unknown-field dropping — keyed
by ``claustrum_process_id`` in its own ``hosted_state.json``.
"""

from __future__ import annotations

import json

from clauster.hosted_state import CURRENT_SCHEMA, HostedStateStore

_REC = {
    "project": "alpha",
    "label": "hosted:alpha",
    "permission_mode": "acceptEdits",
    "claude_session_uuid": "11111111-1111-4111-8111-111111111111",
    "daemon_last_seq": 7,
    "hosted_log_path": "/tmp/alpha.log",
    "agent_pid": 4242,
    "agent_proc_start": 1717000000.0,
    "started_at": "2026-06-12T00:00:00+00:00",
    "intentional_stop": False,
}


def test_round_trip(tmp_path):
    store = HostedStateStore(tmp_path)
    store.save({"pid-1": _REC})
    assert store.load() == {"pid-1": _REC}


def test_missing_file_loads_empty(tmp_path):
    assert HostedStateStore(tmp_path).load() == {}


def test_corrupt_file_loads_empty(tmp_path):
    (tmp_path / "hosted_state.json").write_text("}{ not json", encoding="utf-8")
    assert HostedStateStore(tmp_path).load() == {}


def test_non_utf8_file_tolerated(tmp_path):
    (tmp_path / "hosted_state.json").write_bytes(b"\xff\xfe not utf-8")
    assert HostedStateStore(tmp_path).load() == {}


def test_non_dict_root_returns_empty(tmp_path):
    (tmp_path / "hosted_state.json").write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert HostedStateStore(tmp_path).load() == {}


def test_non_dict_sessions_returns_empty(tmp_path):
    # A current-schema file whose `sessions` is not a map degrades to empty.
    (tmp_path / "hosted_state.json").write_text(
        json.dumps({"schema_version": CURRENT_SCHEMA, "sessions": "garbage"}), encoding="utf-8"
    )
    assert HostedStateStore(tmp_path).load() == {}


def test_non_dict_session_values_are_skipped(tmp_path):
    (tmp_path / "hosted_state.json").write_text(
        json.dumps({"schema_version": CURRENT_SCHEMA, "sessions": {"pid-1": _REC, "junk": ["x"]}}),
        encoding="utf-8",
    )
    assert HostedStateStore(tmp_path).load() == {"pid-1": _REC}


def test_migrate_backup_failure_is_logged_not_silent(tmp_path, caplog, monkeypatch):
    # A failed pre-migration .bak write is surfaced (no silent drop) while the load
    # still succeeds. Monkeypatch the atomic writer rather than chmod (Windows ignores it).
    (tmp_path / "hosted_state.json").write_text(
        json.dumps({"schema_version": 0, "sessions": {"pid-1": _REC}}), encoding="utf-8"
    )

    def boom(target, text):
        raise OSError("simulated: backup write failed")

    # The shared KeyedJsonStore._migrate (in clauster.state) owns the .bak write.
    monkeypatch.setattr("clauster.state.atomic_write_text", boom)
    with caplog.at_level("WARNING", logger="clauster.hosted_state"):
        loaded = HostedStateStore(tmp_path).load()
    assert loaded == {"pid-1": _REC}  # migration still succeeded
    assert not (tmp_path / "hosted_state.json.bak").exists()
    assert any("backup" in r.message for r in caplog.records)


def test_migrate_does_not_overwrite_existing_backup(tmp_path):
    (tmp_path / "hosted_state.json").write_text(
        json.dumps({"schema_version": 0, "sessions": {"pid-1": _REC}}), encoding="utf-8"
    )
    (tmp_path / "hosted_state.json.bak").write_text("ORIGINAL", encoding="utf-8")
    assert HostedStateStore(tmp_path).load() == {"pid-1": _REC}
    assert (tmp_path / "hosted_state.json.bak").read_text(encoding="utf-8") == "ORIGINAL"


def test_unknown_fields_dropped(tmp_path):
    path = tmp_path / "hosted_state.json"
    path.write_text(
        json.dumps(
            {"schema_version": CURRENT_SCHEMA, "sessions": {"pid-1": {**_REC, "bogus": 1}}}
        ),
        encoding="utf-8",
    )
    loaded = HostedStateStore(tmp_path).load()
    assert "bogus" not in loaded["pid-1"]
    assert loaded["pid-1"] == _REC


def test_old_schema_migrates_preserving_sessions_with_backup(tmp_path):
    path = tmp_path / "hosted_state.json"
    path.write_text(
        json.dumps({"schema_version": 0, "sessions": {"pid-1": _REC}}), encoding="utf-8"
    )
    # Migration re-stamps the schema and preserves a well-formed sessions map; a
    # one-time .bak is taken first.
    assert HostedStateStore(tmp_path).load() == {"pid-1": _REC}
    assert (tmp_path / "hosted_state.json.bak").exists()


def test_old_schema_non_dict_sessions_degrades_to_empty(tmp_path):
    path = tmp_path / "hosted_state.json"
    path.write_text(json.dumps({"schema_version": 0, "sessions": "garbage"}), encoding="utf-8")
    assert HostedStateStore(tmp_path).load() == {}


def test_save_is_atomic_and_versioned(tmp_path):
    store = HostedStateStore(tmp_path)
    store.save({"pid-1": _REC})
    payload = json.loads((tmp_path / "hosted_state.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == CURRENT_SCHEMA
    assert payload["sessions"]["pid-1"]["daemon_last_seq"] == 7
    assert not (tmp_path / "hosted_state.json.tmp").exists()  # tmp renamed away
