from __future__ import annotations

import json

import pytest

from clauster.hosted_state import HostedStateStore
from clauster.state import StateStore


def test_save_load_roundtrip(tmp_path):
    store = StateStore(tmp_path)
    data = {"alpha": {"label": "alpha", "intentional_stop": True, "spawn_mode": "same-dir"}}
    store.save(data)
    assert store.load() == data


def test_missing_file_returns_empty(tmp_path):
    assert StateStore(tmp_path).load() == {}


def test_corrupt_json_tolerated(tmp_path):
    (tmp_path / "state.json").write_text("{not valid json")
    assert StateStore(tmp_path).load() == {}


def test_non_utf8_file_tolerated(tmp_path):
    # A non-UTF-8 state.json raises UnicodeDecodeError (a ValueError) on read; the
    # documented "tolerates a corrupt file" contract must still hold.
    (tmp_path / "state.json").write_bytes(b"\xff\xfe\x00not utf-8")
    assert StateStore(tmp_path).load() == {}


# A parse failure that is not a JSONDecodeError. Both used to escape KeyedJsonStore.load
# and propagate out of a startup read, taking the app down instead of degrading to {}.
_HOSTILE = [
    pytest.param("[" * 100_000, id="deeply-nested"),
    pytest.param("1" * 5000, id="oversized-int-literal"),
]

# Every KeyedJsonStore subclass shares the one handler, so both must degrade identically.
_STORES = [
    pytest.param(StateStore, "state.json", id="state"),
    pytest.param(HostedStateStore, "hosted_state.json", id="hosted"),
]


def test_oversized_int_literal_is_a_bare_value_error():
    # Positive control for the parametrized case below: the base-10
    # integer-string-conversion limit (CVE-2020-10735) is settable at runtime
    # (sys.set_int_max_str_digits, PYTHONINTMAXSTRDIGITS), and with it disabled
    # json.loads would return an int and the load would degrade at the isinstance
    # check instead — green for the wrong reason. Pin that the arm is reachable.
    with pytest.raises(ValueError) as raised:
        json.loads("1" * 5000)
    assert not isinstance(raised.value, json.JSONDecodeError)


def test_deeply_nested_json_is_a_recursion_error():
    # The other positive control: CPython's recursive scanner overflows before json
    # can raise JSONDecodeError, and RecursionError is not a ValueError.
    with pytest.raises(RecursionError):
        json.loads("[" * 100_000)


@pytest.mark.parametrize(("store_cls", "filename"), _STORES)
@pytest.mark.parametrize("payload", _HOSTILE)
def test_hostile_payload_degrades_to_empty(tmp_path, store_cls, filename, payload):
    (tmp_path / filename).write_text(payload, encoding="utf-8")
    assert store_cls(tmp_path).load() == {}


@pytest.mark.parametrize(("store_cls", "filename"), _STORES)
@pytest.mark.parametrize("payload", _HOSTILE)
def test_hostile_payload_leaves_the_file_recoverable(tmp_path, store_cls, filename, payload):
    # Degrading must not consume the operator's only copy: the load bails before
    # _migrate, so no .bak is taken and the file on disk is byte-identical.
    path = tmp_path / filename
    path.write_text(payload, encoding="utf-8")
    store_cls(tmp_path).load()
    assert path.read_text(encoding="utf-8") == payload
    assert not (tmp_path / f"{filename}.bak").exists()


def test_unknown_fields_dropped(tmp_path):
    (tmp_path / "state.json").write_text(
        json.dumps({"schema_version": 1, "instances": {"a": {"label": "a", "bogus": 1}}})
    )
    assert StateStore(tmp_path).load() == {"a": {"label": "a"}}


def test_old_schema_migrates_with_backup(tmp_path):
    (tmp_path / "state.json").write_text(
        json.dumps({"schema_version": 0, "instances": {"a": {"label": "a"}}})
    )
    loaded = StateStore(tmp_path).load()
    assert loaded == {"a": {"label": "a"}}
    assert (tmp_path / "state.json.bak").exists()


def test_migrate_backup_failure_is_logged_not_silent(tmp_path, caplog, monkeypatch):
    # A failed pre-migration .bak write must be surfaced (audit: no silent drops),
    # while the load still succeeds (the backup is best-effort). Monkeypatch the
    # atomic writer rather than chmod — Windows ignores a dir's write bit, and that
    # cell is merge-blocking.
    (tmp_path / "state.json").write_text(
        json.dumps({"schema_version": 0, "instances": {"a": {"label": "a"}}}), encoding="utf-8"
    )

    def boom(target, text):
        raise OSError("simulated: backup write failed")

    monkeypatch.setattr("clauster.state.atomic_write_text", boom)
    with caplog.at_level("WARNING", logger="clauster.state"):
        loaded = StateStore(tmp_path).load()
    assert loaded == {"a": {"label": "a"}}  # migration still succeeded
    assert not (tmp_path / "state.json.bak").exists()  # backup genuinely failed
    assert any("backup" in r.message for r in caplog.records)  # surfaced, not silent


def test_non_dict_root_returns_empty(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps(["not", "a", "dict"]))
    assert StateStore(tmp_path).load() == {}


def test_non_dict_instance_values_are_skipped(tmp_path):
    # A well-formed file whose `instances` map has a non-dict value (e.g. a list)
    # for one project drops that entry while keeping the valid ones — no crash.
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "instances": {"good": {"label": "g"}, "junk": ["not", "a", "dict"]},
            }
        )
    )
    assert StateStore(tmp_path).load() == {"good": {"label": "g"}}


def test_migrate_does_not_overwrite_existing_backup(tmp_path):
    # When a .bak already exists, migration must NOT clobber it (the pre-migration
    # snapshot is taken once). The migration still coerces and loads the legacy data.
    (tmp_path / "state.json").write_text(
        json.dumps({"schema_version": 0, "instances": {"a": {"label": "a"}}})
    )
    (tmp_path / "state.json.bak").write_text("ORIGINAL BACKUP")
    loaded = StateStore(tmp_path).load()
    assert loaded == {"a": {"label": "a"}}
    assert (tmp_path / "state.json.bak").read_text() == "ORIGINAL BACKUP"  # preserved
