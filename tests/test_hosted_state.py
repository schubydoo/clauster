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
