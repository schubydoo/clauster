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

from clauster import atomicio
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


# Each is a ~/.claude.json that exists but is not a JSON object. The sentinel stands in
# for a token: it must never reach the error message.
_SECRET = "SENTINEL-oauth-token-0123"
UNPARSEABLE = [
    pytest.param(b'{"oauthAccount": {"token": "' + _SECRET.encode() + b'"', id="truncated"),
    pytest.param(b'{"projects": {,}, "token": "' + _SECRET.encode() + b'"}', id="malformed"),
    pytest.param(b"", id="empty"),
    pytest.param(b'{"token": "' + _SECRET.encode() + b'\xff\xfe"}', id="non-utf8"),
    # A >4300-digit int literal raises a bare ValueError, not a JSONDecodeError.
    pytest.param(b'{"n": ' + b"1" * 5000 + b"}", id="oversized-int"),
    # Deep nesting raises RecursionError, which is not a ValueError at all.
    pytest.param(b"[" * 100_000, id="deeply-nested"),
    pytest.param(b'["not", "a", "dict"]', id="array-root"),
    pytest.param(b'"' + _SECRET.encode() + b'"', id="string-root"),
    pytest.param(b"5", id="number-root"),
    pytest.param(b"null", id="null-root"),
]


@pytest.mark.parametrize("content", UNPARSEABLE)
def test_update_claude_json_refuses_an_unparseable_file(tmp_path: Path, content: bytes) -> None:
    # #1600: this used to parse as {} and replace the file with only the touched keys,
    # with no backup. Now it raises before mutate and before any write.
    f = tmp_path / "claude.json"
    f.write_bytes(content)
    calls: list[dict] = []

    with pytest.raises(cj.ClaudeJsonUnparseable) as info:
        cj.update_claude_json(f, lambda data: calls.append(data))

    assert f.read_bytes() == content  # byte-identical
    assert calls == []  # mutate never ran
    assert not f.with_suffix(f.suffix + ".bak").exists()  # nothing written beside it
    assert list(tmp_path.glob("claude.json.*.tmp")) == []
    message = str(info.value)
    assert str(f) in message and "Clauster did not change it" in message
    assert _SECRET not in message  # the reason never quotes the file's content
    assert isinstance(info.value, ValueError)  # callers catching ValueError still do


def test_update_claude_json_unparseable_leaves_an_existing_backup_alone(tmp_path: Path) -> None:
    f = tmp_path / "claude.json"
    f.write_bytes(b"{truncated")
    backup = f.with_suffix(f.suffix + ".bak")
    backup.write_bytes(b'{"older": true}')

    with pytest.raises(cj.ClaudeJsonUnparseable):
        cj.update_claude_json(f, lambda data: data.__setitem__("k", 1))

    assert backup.read_bytes() == b'{"older": true}'


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b'{"a": 1,\n  "b": }', "invalid JSON at line 2 column 8"),
        (b"\xff", "not UTF-8 text"),
        (b'{"n": ' + b"1" * 5000 + b"}", "a value that cannot be parsed"),
        # Python 3.14 bounds the scanner by real stack use, not a depth counter: with a stack
        # rlimit above 8 MB (the ubuntu CI runner) all 100,000 levels fit and the parse
        # fails at end of input instead. The RecursionError arm is pinned in the next test.
        (b"[" * 100_000, "nested too deeply|invalid JSON at line 1 column 100001"),
        (b"[]", "the top level is not a JSON object"),
    ],
    # Explicit ids: pytest puts the test id in PYTEST_CURRENT_TEST, and a 100,000-byte
    # payload id exceeds the 32,767-character Windows limit for one environment variable.
    ids=["malformed", "non-utf8", "oversized-int", "deeply-nested", "array-root"],
)
def test_unparseable_reason_names_the_failure(tmp_path: Path, content: bytes, reason: str) -> None:
    f = tmp_path / "claude.json"
    f.write_bytes(content)
    with pytest.raises(cj.ClaudeJsonUnparseable, match=reason):
        cj.update_claude_json(f, lambda data: None)


def test_recursion_error_names_the_nesting(tmp_path: Path, monkeypatch) -> None:
    # Pins the RecursionError arm on every interpreter and stack size.
    f = tmp_path / "claude.json"
    f.write_bytes(b"{}")

    def _overflow(_text: str) -> None:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(cj.json, "loads", _overflow)
    with pytest.raises(cj.ClaudeJsonUnparseable, match="nested too deeply"):
        cj.update_claude_json(f, lambda data: None)
    assert f.read_bytes() == b"{}"


def test_update_claude_json_valid_object_still_writes(tmp_path: Path) -> None:
    # Positive control for the refusal above: the same call on a parseable object writes.
    f = tmp_path / "claude.json"
    f.write_bytes(b'{"keep": 1}')

    assert cj.update_claude_json(f, lambda data: data.__setitem__("k", 1)) is True

    assert json.loads(f.read_text(encoding="utf-8")) == {"keep": 1, "k": 1}


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


# --- #1171: the flock lives in the state dir, never as a sidecar beside the target ---


def test_locked_write_leaves_no_lock_sidecar_beside_the_target(tmp_path: Path) -> None:
    # Regression for #1171: a project `.claude/settings.json` sits inside the user's
    # git-tracked tree, so a `<file>.lock` sidecar there is a commit-hygiene hazard.
    project = tmp_path / "proj" / ".claude"
    project.mkdir(parents=True)
    atomicio.configure_lock_dir(tmp_path / "state" / "locks")
    f = project / "settings.json"

    cj.locked_replace_json_file(f, lambda raw: {"k": 1}, render=json.dumps)

    assert json.loads(f.read_text(encoding="utf-8")) == {"k": 1}
    assert sorted(p.name for p in project.iterdir()) == ["settings.json"]


@pytest.mark.skipif(os.name != "posix", reason="advisory flock is POSIX-only (no fcntl)")
def test_locked_flock_lands_in_the_configured_state_lock_dir(tmp_path: Path) -> None:
    # The relocation is not just "no sidecar": the flock is still taken, on a file keyed
    # to the target inside the deployment state dir (the #915 primitive).
    lock_dir = tmp_path / "state" / "locks"
    atomicio.configure_lock_dir(lock_dir)
    f = tmp_path / "claude.json"

    cj.update_claude_json(f, lambda data: data.__setitem__("k", 1))

    assert [p.name for p in lock_dir.iterdir()] == [atomicio._cross_process_lock_file(f).name]
    assert not f.with_suffix(f.suffix + ".lock").exists()


def test_locked_write_still_lands_when_no_lock_dir_is_configured(tmp_path: Path, caplog) -> None:
    # Degrade loudly, never silently: with no lock dir the cross-process flock is skipped
    # (the in-process lock still holds, the atomic replace still protects the file) and a
    # warning is emitted. The autouse fixture leaves atomicio unconfigured.
    f = tmp_path / "claude.json"
    with caplog.at_level("WARNING", logger="clauster.atomicio"):
        cj.update_claude_json(f, lambda data: data.__setitem__("k", 1))

    assert json.loads(f.read_text(encoding="utf-8")) == {"k": 1}
    if os.name == "posix":  # the warning is on the flock path, which Windows never enters
        assert any("lock dir not configured" in r.message for r in caplog.records)


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
