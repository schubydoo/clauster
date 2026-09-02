from __future__ import annotations

import json
from pathlib import Path

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
def test_hostile_payload_is_copied_aside_and_logged(
    tmp_path, store_cls, filename, payload, caplog
):
    # Degrading must not consume the operator's only copy, and must not be silent.
    # A caller can overwrite the file straight after a degraded load (ops.migrate_state
    # does), so the one-time .corrupt.bak is what keeps the bytes recoverable.
    path = tmp_path / filename
    path.write_text(payload, encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert store_cls(tmp_path).load() == {}
    assert path.read_text(encoding="utf-8") == payload  # load itself never rewrites
    assert (tmp_path / f"{filename}.corrupt.bak").read_text(encoding="utf-8") == payload
    assert any("ignoring corrupt" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(("store_cls", "filename"), _STORES)
def test_non_utf8_file_is_copied_aside_and_logged(tmp_path, store_cls, filename, caplog):
    # A non-UTF-8 file is the third corrupt shape and degrades through the same
    # contract. It is why the copy is a byte-level copy and not a re-write: the decode
    # is what failed, so there is no text form of these bytes to write back out.
    blob = b"\xff\xfe\x00not utf-8"
    (tmp_path / filename).write_bytes(blob)
    with caplog.at_level("WARNING"):
        assert store_cls(tmp_path).load() == {}
    assert (tmp_path / f"{filename}.corrupt.bak").read_bytes() == blob
    assert any("ignoring corrupt" in r.getMessage() for r in caplog.records)


def test_corrupt_copy_is_byte_exact_not_newline_normalized(tmp_path):
    # A copy, not a read_text -> write round-trip: read_text applies universal newlines
    # on every OS, so a re-write would silently rewrite CRLF (and a lone CR) to LF. In a
    # file we have just declared uninterpretable, a stray control byte can be anywhere,
    # and byte fidelity is the whole point of keeping it.
    blob = b'{"schema_version": 1,\r\n "instances": \r{ oops'
    (tmp_path / "state.json").write_bytes(blob)
    assert StateStore(tmp_path).load() == {}
    assert (tmp_path / "state.json.corrupt.bak").read_bytes() == blob


@pytest.mark.parametrize("payload", _HOSTILE)
def test_hostile_payload_does_not_clobber_an_existing_copy(tmp_path, payload):
    # Same rule the schema migration follows: an existing copy is the older, more
    # original one. Discarding a corrupt file must never overwrite it.
    (tmp_path / "state.json").write_text(payload, encoding="utf-8")
    (tmp_path / "state.json.corrupt.bak").write_text("ORIGINAL", encoding="utf-8")
    assert StateStore(tmp_path).load() == {}
    assert (tmp_path / "state.json.corrupt.bak").read_text(encoding="utf-8") == "ORIGINAL"


def test_corrupt_copy_does_not_consume_the_pre_migration_backup_slot(tmp_path):
    # The two copies guard different things, so they get different names. If they shared
    # one slot, a corrupt file seen once would claim it, and the operator's later repair
    # to a legacy schema would then be coerced with NO pre-migration snapshot.
    sj = tmp_path / "state.json"
    sj.write_text("[" * 100_000, encoding="utf-8")
    assert StateStore(tmp_path).load() == {}
    assert (tmp_path / "state.json.corrupt.bak").exists()
    assert not (tmp_path / "state.json.bak").exists()

    legacy = json.dumps({"schema_version": 0, "instances": {"a": {"label": "a"}}})
    sj.write_text(legacy, encoding="utf-8")
    assert StateStore(tmp_path).load() == {"a": {"label": "a"}}
    assert (tmp_path / "state.json.bak").read_text(encoding="utf-8") == legacy


def test_unreadable_file_is_logged_with_no_copy(tmp_path, caplog, monkeypatch):
    # An OSError is an unreadable file (permissions, IO), not malformed content: there
    # is nothing to copy, and it is the arm actually worth diagnosing. Monkeypatch
    # rather than chmod -- Windows ignores a file's read bit, and that cell is
    # merge-blocking.
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise OSError("simulated: EIO")

    monkeypatch.setattr(Path, "read_text", boom)
    with caplog.at_level("WARNING", logger="clauster.state"):
        assert StateStore(tmp_path).load() == {}
    assert any("could not read" in r.getMessage() for r in caplog.records)
    assert not (tmp_path / "state.json.corrupt.bak").exists()


def test_corrupt_copy_failure_is_logged_not_silent(tmp_path, caplog, monkeypatch):
    # The copy is best-effort -- it must never block the load or abort a caller's
    # transaction -- but a failure to keep the only copy is exactly the kind of loss
    # that must not pass silently.
    (tmp_path / "state.json").write_text("[" * 100_000, encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise OSError("simulated: copy failed")

    monkeypatch.setattr("clauster.state.shutil.copy2", boom)
    with caplog.at_level("WARNING", logger="clauster.state"):
        assert StateStore(tmp_path).load() == {}
    assert any("could not copy corrupt" in r.getMessage() for r in caplog.records)
    assert not (tmp_path / "state.json.corrupt.bak").exists()


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
