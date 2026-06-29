"""Shared ~/.claude.json writer primitive (factored out of trust.py, #687).

Every test passes an explicit ``claude_json = tmp_path / "claude.json"`` (never the
real ``CLAUDE_JSON``) and runs under the autouse HOME-isolation fixture, so the live
account can never be touched — the same belt-and-suspenders the trust tests use.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from clauster import claude_json as cj


def test_update_claude_json_merges_subtree_preserving_siblings(tmp_path: Path) -> None:
    f = tmp_path / "claude.json"
    f.write_text(json.dumps({"projects": {"/p": {"x": 1}}, "misc": 7}), encoding="utf-8")

    def mutate(data: dict) -> None:
        data.setdefault("mcpServers", {})["srv"] = {"command": "/bin/foo"}

    changed = cj.update_claude_json(f, mutate)

    assert changed is True
    out = json.loads(f.read_text(encoding="utf-8"))
    assert out["mcpServers"]["srv"]["command"] == "/bin/foo"  # subtree written
    assert out["projects"]["/p"]["x"] == 1  # every sibling key preserved
    assert out["misc"] == 7


def test_update_claude_json_skips_write_when_mutator_returns_false(tmp_path: Path) -> None:
    f = tmp_path / "claude.json"
    f.write_text(json.dumps({"a": 1}), encoding="utf-8")
    before = f.read_text(encoding="utf-8")

    changed = cj.update_claude_json(f, lambda data: False)

    assert changed is False
    assert f.read_text(encoding="utf-8") == before  # untouched
    assert not f.with_suffix(f.suffix + ".bak").exists()  # no write ⇒ no backup


def test_update_claude_json_creates_missing_file(tmp_path: Path) -> None:
    f = tmp_path / "claude.json"  # does not exist

    cj.update_claude_json(f, lambda data: data.__setitem__("k", "v"))

    assert json.loads(f.read_text(encoding="utf-8")) == {"k": "v"}


def test_update_claude_json_takes_backup_once(tmp_path: Path) -> None:
    f = tmp_path / "claude.json"
    f.write_text(json.dumps({"a": 1}), encoding="utf-8")

    cj.update_claude_json(f, lambda data: data.__setitem__("b", 2))
    cj.update_claude_json(f, lambda data: data.__setitem__("c", 3))

    backup = f.with_suffix(f.suffix + ".bak")
    assert json.loads(backup.read_text(encoding="utf-8")) == {"a": 1}  # first snapshot only


def test_update_claude_json_non_dict_root_coerced(tmp_path: Path) -> None:
    f = tmp_path / "claude.json"
    f.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")

    cj.update_claude_json(f, lambda data: data.__setitem__("k", 1))

    assert json.loads(f.read_text(encoding="utf-8")) == {"k": 1}


def test_update_claude_json_unreadable_propagates_not_clobbered(
    tmp_path: Path, monkeypatch
) -> None:
    import pathlib

    f = tmp_path / "claude.json"
    original = json.dumps({"keep": True})
    f.write_text(original, encoding="utf-8")
    real_read_text = pathlib.Path.read_text

    def boom(self, *a, **k):
        if self.name == "claude.json":
            raise PermissionError("simulated")
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "read_text", boom)
    with pytest.raises(PermissionError):
        cj.update_claude_json(f, lambda data: data.__setitem__("x", 1))
    monkeypatch.undo()
    assert f.read_text(encoding="utf-8") == original  # nothing clobbered


def test_update_claude_json_uses_unique_temp(tmp_path: Path) -> None:
    f = tmp_path / "claude.json"
    f.write_text("{}", encoding="utf-8")

    cj.update_claude_json(f, lambda data: data.__setitem__("k", 1))

    assert not f.with_suffix(f.suffix + ".tmp").exists()  # no fixed-name temp
    assert list(tmp_path.glob("claude.json.*.tmp")) == []  # unique temp consumed


needs_posix = pytest.mark.skipif(os.name != "posix", reason="POSIX file-mode semantics")


@needs_posix
def test_update_claude_json_preserves_existing_mode(tmp_path: Path) -> None:
    f = tmp_path / "claude.json"
    f.write_text("{}", encoding="utf-8")
    f.chmod(0o644)

    cj.update_claude_json(f, lambda data: data.__setitem__("k", 1))

    assert stat.S_IMODE(f.stat().st_mode) == 0o644  # not reset to 0600


@needs_posix
def test_update_claude_json_new_file_owner_only(tmp_path: Path) -> None:
    f = tmp_path / "claude.json"  # does not exist

    cj.update_claude_json(f, lambda data: data.__setitem__("k", 1))

    assert stat.S_IMODE(f.stat().st_mode) == 0o600  # it can hold tokens


def test_update_claude_json_non_posix_skips_chmod(tmp_path: Path, monkeypatch) -> None:
    # On a non-POSIX platform the mode-mirroring fchmod is skipped (Windows permissions are
    # ACL-based); the atomic write still completes. Patch the _is_posix seam (not os.name,
    # which would break tempfile) to cover the non-POSIX branch on a POSIX host.
    monkeypatch.setattr(cj, "_is_posix", lambda: False)
    f = tmp_path / "claude.json"
    f.write_text('{"a": 1}', encoding="utf-8")

    cj.update_claude_json(f, lambda data: data.__setitem__("b", 2))

    assert json.loads(f.read_text(encoding="utf-8")) == {"a": 1, "b": 2}


def test_locked_noop_without_fcntl(tmp_path: Path, monkeypatch) -> None:
    # Where fcntl is unavailable (Windows) the lock degrades to a no-op: the write
    # still completes and no .lock sidecar is created.
    monkeypatch.setattr(cj, "fcntl", None)
    f = tmp_path / "claude.json"
    f.write_text("{}", encoding="utf-8")

    cj.update_claude_json(f, lambda data: data.__setitem__("k", 1))

    assert json.loads(f.read_text(encoding="utf-8")) == {"k": 1}
    assert not f.with_suffix(f.suffix + ".lock").exists()


@pytest.mark.skipif(cj.fcntl is None, reason="advisory flock is POSIX-only (no fcntl)")
def test_locked_lockfile_open_failure_is_best_effort(tmp_path: Path, monkeypatch, caplog) -> None:
    # If the .lock sidecar can't be opened, never block the write — proceed unlocked
    # (the atomic replace still protects the file) and surface a warning.
    f = tmp_path / "claude.json"
    f.write_text("{}", encoding="utf-8")
    real_open = os.open

    def boom(path, *a, **k):
        if str(path).endswith(".lock"):
            raise OSError("simulated: cannot open lock file")
        return real_open(path, *a, **k)

    monkeypatch.setattr(cj.os, "open", boom)
    with caplog.at_level("WARNING", logger="clauster.claude_json"):
        cj.update_claude_json(f, lambda data: data.__setitem__("k", 1))

    assert json.loads(f.read_text(encoding="utf-8")) == {"k": 1}  # write still landed
    assert any("without a lock" in r.message for r in caplog.records)


# --- locked_replace_json_file: the project-file (non-~/.claude.json) primitive ----


def test_locked_replace_creates_missing_file(tmp_path: Path) -> None:
    f = tmp_path / ".mcp.json"  # absent
    cj.locked_replace_json_file(
        f,
        lambda raw: {"mcpServers": {"s": {"command": "x"}}},
        render=lambda d: json.dumps(d) + "\n",
    )
    assert json.loads(f.read_text(encoding="utf-8")) == {"mcpServers": {"s": {"command": "x"}}}
    assert not f.with_suffix(f.suffix + ".bak").exists()  # no prior content ⇒ no backup


def test_locked_replace_sees_current_bytes_and_backs_up_once(tmp_path: Path) -> None:
    f = tmp_path / ".mcp.json"
    f.write_text('{"old": 1}', encoding="utf-8")
    seen: list[bytes] = []

    def mutate(raw: bytes) -> dict:
        seen.append(raw)
        return {"new": 2}

    cj.locked_replace_json_file(f, mutate, render=lambda d: json.dumps(d))
    assert seen == [b'{"old": 1}']  # mutate receives the verbatim current bytes
    assert json.loads(f.read_text(encoding="utf-8")) == {"new": 2}
    assert f.with_suffix(f.suffix + ".bak").read_text(encoding="utf-8") == '{"old": 1}'


def test_locked_replace_mutate_raise_aborts_without_writing(tmp_path: Path) -> None:
    f = tmp_path / ".mcp.json"
    f.write_text('{"keep": 1}', encoding="utf-8")

    def mutate(raw: bytes) -> dict:
        raise ValueError("abort")

    with pytest.raises(ValueError, match="abort"):
        cj.locked_replace_json_file(f, mutate, render=json.dumps)
    assert json.loads(f.read_text(encoding="utf-8")) == {"keep": 1}  # untouched


needs_posix2 = pytest.mark.skipif(os.name != "posix", reason="POSIX file-mode semantics")


@needs_posix2
def test_locked_replace_preserves_existing_mode(tmp_path: Path) -> None:
    f = tmp_path / ".mcp.json"
    f.write_text("{}", encoding="utf-8")
    os.chmod(f, 0o600)
    cj.locked_replace_json_file(f, lambda raw: {"k": 1}, render=json.dumps)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600  # not loosened by the replace


def test_locked_replace_backup_failure_is_warned_not_fatal(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    # A failed .bak is best-effort: warn, never block the write (mirrors the ~/.claude.json path).
    f = tmp_path / ".mcp.json"
    f.write_text('{"old": 1}', encoding="utf-8")
    real_write_text = Path.write_text

    def boom(self, *a, **k):
        if self.name.endswith(".bak"):
            raise OSError("simulated: cannot write backup")
        return real_write_text(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", boom)
    with caplog.at_level("WARNING", logger="clauster.claude_json"):
        cj.locked_replace_json_file(f, lambda raw: {"new": 2}, render=json.dumps)
    assert json.loads(f.read_text(encoding="utf-8")) == {"new": 2}  # write still landed
    assert any("backup" in r.message for r in caplog.records)


def test_locked_replace_backup_taken_once(tmp_path: Path) -> None:
    # The .bak is written once (before the first modification) and a later write does NOT
    # clobber it — the original pre-edit content is preserved across repeated writes.
    f = tmp_path / ".mcp.json"
    f.write_text('{"v": 1}', encoding="utf-8")
    cj.locked_replace_json_file(f, lambda raw: {"v": 2}, render=json.dumps)
    cj.locked_replace_json_file(f, lambda raw: {"v": 3}, render=json.dumps)
    assert json.loads(f.read_text(encoding="utf-8")) == {"v": 3}
    # .bak still holds the ORIGINAL content, not the intermediate {"v": 2}.
    assert f.with_suffix(f.suffix + ".bak").read_text(encoding="utf-8") == '{"v": 1}'


def test_locked_replace_non_posix_skips_chmod(tmp_path: Path, monkeypatch) -> None:
    # On Windows file permissions are ACL-based, so the POSIX mode-preservation branch is
    # skipped; the atomic write still completes. Patch the _is_posix seam (not os.name,
    # which would break tempfile), mirroring test_update_claude_json_non_posix_skips_chmod.
    monkeypatch.setattr(cj, "_is_posix", lambda: False)
    f = tmp_path / ".mcp.json"
    cj.locked_replace_json_file(f, lambda raw: {"k": 1}, render=json.dumps)
    assert json.loads(f.read_text(encoding="utf-8")) == {"k": 1}
