from __future__ import annotations

import json

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
    # write rather than chmod — Windows ignores a dir's write bit, and that cell
    # is merge-blocking.
    import pathlib

    (tmp_path / "state.json").write_text(
        json.dumps({"schema_version": 0, "instances": {"a": {"label": "a"}}}), encoding="utf-8"
    )
    real_write_text = pathlib.Path.write_text

    def boom(self, *args, **kwargs):
        if self.suffix == ".bak":
            raise OSError("simulated: backup write failed")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", boom)
    with caplog.at_level("WARNING", logger="clauster.state"):
        loaded = StateStore(tmp_path).load()
    assert loaded == {"a": {"label": "a"}}  # migration still succeeded
    assert not (tmp_path / "state.json.bak").exists()  # backup genuinely failed
    assert any("backup" in r.message for r in caplog.records)  # surfaced, not silent


def test_non_dict_root_returns_empty(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps(["not", "a", "dict"]))
    assert StateStore(tmp_path).load() == {}
