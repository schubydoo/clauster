from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from clauster import state
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
# (The deeply-nested one raises RecursionError -- not a ValueError -- on every supported
# interpreter; the oversized int raises a bare ValueError. Both reach the same handler.)
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


def test_deeply_nested_json_raises_before_it_can_be_parsed():
    # The other positive control: the deeply-nested input raises before json returns a
    # value, so it never degrades at the isinstance check. On every supported interpreter
    # tested that is RecursionError -- not a ValueError, which is exactly how it escaped the
    # old handler -- but the raises tuple also accepts JSONDecodeError defensively, so this
    # control never fails if an interpreter reports the overflow as a decode error. The test
    # below pins the RecursionError arm independently of this one.
    with pytest.raises((RecursionError, json.JSONDecodeError)):
        json.loads("[" * 100_000)


def test_recursion_error_degrades_on_any_interpreter(tmp_path, caplog, monkeypatch):
    # Pins the RecursionError half of the handler directly, independent of the scanner's
    # exact error text: the deeply-nested input raises RecursionError on every supported
    # interpreter, and this arm is load-bearing. Coverage cannot catch its loss -- the
    # tuple is one line.
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")

    def _overflow(_text):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(state.json, "loads", _overflow)
    with caplog.at_level("WARNING", logger="clauster.state"):
        assert StateStore(tmp_path).load() == {}
    assert any("RecursionError" in r.getMessage() for r in caplog.records)
    assert (tmp_path / "state.json.corrupt.bak").read_text(encoding="utf-8") == "{}"


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


def test_unreadable_file_raises_out_of_the_strict_read(tmp_path, monkeypatch):
    # An unreadable file is not an empty one. `save` needs only the DIRECTORY to be
    # writable, so a caller that writes back what it read would replace a file it never
    # managed to read -- the same wipe as a corrupt one, by a different route. The
    # DB-backed store raises OSError here for the same reason (#949).
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise OSError("simulated: EIO")

    monkeypatch.setattr(Path, "read_text", boom)
    with pytest.raises(OSError, match="simulated: EIO"):
        StateStore(tmp_path).load_strict()


def test_missing_file_is_empty_not_unreadable(tmp_path):
    # FileNotFoundError is an OSError, so it has to be caught above the arm that lets
    # one through: an absent store is an empty store on BOTH reads.
    assert StateStore(tmp_path).load_strict() == {}


def test_corrupt_copy_failure_is_logged_not_silent(tmp_path, caplog, monkeypatch):
    # The copy is best-effort -- it must never block the load or abort a caller's
    # transaction -- but a failure to keep the only copy is exactly the kind of loss
    # that must not pass silently. atomicio raises only OSError subclasses out of its
    # write path (a read-only mount, EMFILE at mkstemp, a Windows sharing violation),
    # so one arm catches every way the copy can fail.
    (tmp_path / "state.json").write_text("[" * 100_000, encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise PermissionError("simulated: read-only state dir")

    monkeypatch.setattr("clauster.state.atomic_copy_file", boom)
    with caplog.at_level("WARNING", logger="clauster.state"):
        assert StateStore(tmp_path).load() == {}
    # The exception TYPE is the part worth naming -- a read-only mount and a sharing
    # violation degrade identically, so the type is all that tells them apart in a log.
    # Asserting only the prefix would let the line drop back to a bare `exc`.
    assert any(
        "could not copy corrupt" in r.getMessage()
        and "PermissionError: simulated: read-only state dir" in r.getMessage()
        for r in caplog.records
    )
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["state.json"]


def test_corrupt_copy_does_not_swallow_an_interrupt(tmp_path, monkeypatch):
    # Best-effort covers OSError only. A KeyboardInterrupt mid-copy must still
    # propagate rather than be swallowed into a degraded load.
    (tmp_path / "state.json").write_text("[" * 100_000, encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("clauster.state.atomic_copy_file", boom)
    with pytest.raises(KeyboardInterrupt):
        StateStore(tmp_path).load()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_corrupt_copy_is_owner_only_even_from_a_permissive_source(tmp_path):
    # A legacy file predating the atomic writer (or restored through _safe_extract_tar,
    # which writes members with a bare open) sits at the umask default. Copying its mode
    # onto the copy would publish a hosted store's claude session uuid to every local
    # user, so the copy is written through a mkstemp temp instead.
    src = tmp_path / "hosted_state.json"
    src.write_text("[" * 100_000, encoding="utf-8")
    src.chmod(0o644)
    assert HostedStateStore(tmp_path).load() == {}
    mode = (tmp_path / "hosted_state.json.corrupt.bak").stat().st_mode
    assert not mode & (stat.S_IRWXG | stat.S_IRWXO)


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


# Valid JSON that parses to something this store cannot use. It is the third corrupt
# shape, and it reaches the same destructive re-save as the other two, so it degrades
# identically: warn, copy aside once, return {}.
_WRONG_SHAPE = [
    pytest.param(json.dumps(["not", "a", "dict"]), id="root-is-a-list"),
    pytest.param(json.dumps("just a string"), id="root-is-a-string"),
    pytest.param(json.dumps({"schema_version": 1, "instances": ["a"]}), id="map-is-a-list"),
    pytest.param(json.dumps({"schema_version": 1, "instances": None}), id="map-is-null"),
    # The same damage under a MISMATCHED schema_version. _migrate coerces a non-object
    # map to {} on its way past, so checking the shape after it would launder real
    # damage into a clean-looking empty store -- and `clauster migrate` would write that
    # back over the file. Byte-identical damage must get the same verdict either way.
    pytest.param(
        json.dumps({"schema_version": 0, "instances": ["a"]}), id="legacy-schema-map-is-a-list"
    ),
    pytest.param(
        json.dumps({"schema_version": 0, "instances": None}), id="legacy-schema-map-is-null"
    ),
]


@pytest.mark.parametrize("payload", _WRONG_SHAPE)
def test_wrong_shape_is_copied_aside_and_logged(tmp_path, caplog, payload):
    path = tmp_path / "state.json"
    path.write_text(payload, encoding="utf-8")
    with caplog.at_level("WARNING", logger="clauster.state"):
        assert StateStore(tmp_path).load() == {}
    assert path.read_text(encoding="utf-8") == payload  # load itself never rewrites
    assert (tmp_path / "state.json.corrupt.bak").read_text(encoding="utf-8") == payload
    assert any("ignoring corrupt" in r.getMessage() for r in caplog.records)
    # Rejected before the schema branch, so no migration ran and no snapshot was taken.
    assert not (tmp_path / "state.json.bak").exists()


@pytest.mark.parametrize("payload", _WRONG_SHAPE)
def test_wrong_shape_reason_names_the_shape_not_the_content(caplog, tmp_path, payload):
    # Positive control on the message: the reason must describe the shape, so a reader
    # can tell these apart from a parse failure, and it must never quote the file --
    # `hosted_state.json` carries a claude session uuid.
    (tmp_path / "state.json").write_text(payload, encoding="utf-8")
    with caplog.at_level("WARNING", logger="clauster.state"):
        StateStore(tmp_path).load()
    reasons = [r.getMessage() for r in caplog.records if "ignoring corrupt" in r.getMessage()]
    assert len(reasons) == 1
    assert "not an object" in reasons[0]
    assert payload not in reasons[0]


@pytest.mark.parametrize(("store_cls", "filename"), _STORES)
def test_wrong_shape_reason_names_each_store_own_map_key(tmp_path, caplog, store_cls, filename):
    # The record-map check is shared code reading a per-store class attribute, so both
    # subclasses must reject their own key and say which one it was.
    map_key = store_cls._MAP_KEY
    (tmp_path / filename).write_text(
        json.dumps({"schema_version": 1, map_key: ["not", "an", "object"]}), encoding="utf-8"
    )
    with caplog.at_level("WARNING", logger="clauster"):
        assert store_cls(tmp_path).load() == {}
    assert any(f"{map_key!r} is a list" in r.getMessage() for r in caplog.records)
    assert (tmp_path / f"{filename}.corrupt.bak").exists()


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(json.dumps({"schema_version": 1}), id="map-absent"),
        pytest.param(json.dumps({"schema_version": 1, "instances": {}}), id="map-empty"),
    ],
)
def test_empty_or_absent_record_map_is_not_corruption(tmp_path, caplog, payload):
    # The boundary the shape check must not cross. A schema-correct file that has never
    # held a record looks exactly like this, and `save({})` writes the empty form on
    # every stop, so treating it as damage would warn and copy on ordinary operation.
    (tmp_path / "state.json").write_text(payload, encoding="utf-8")
    with caplog.at_level("WARNING", logger="clauster.state"):
        assert StateStore(tmp_path).load() == {}
    assert not (tmp_path / "state.json.corrupt.bak").exists()
    assert not [r for r in caplog.records if "ignoring corrupt" in r.getMessage()]


def test_wrong_shape_survives_the_round_trip_of_a_real_save(tmp_path):
    # The same control from the other side: what `save` writes must load back clean,
    # with no warning and no copy. If the shape check ever rejects our own output,
    # every ordinary load starts copying the store aside.
    store = StateStore(tmp_path)
    store.save({})
    assert store.load() == {}
    assert not (tmp_path / "state.json.corrupt.bak").exists()


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
