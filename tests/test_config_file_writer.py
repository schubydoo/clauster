"""Path-contained atomic file/dir writer primitive (#766, over the #347 Foundation).

Covers strict path containment (traversal / absolute-path / symlink-escape all
REJECTED before any I/O), the file writer's atomic create/replace, the redacted read
path, the directory-tree writer's atomic create/replace (including the two-rename
swap for an existing tree), delete (file + tree, idempotent), and the advisory flock
serializing concurrent writers on the same target.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from clauster import config_file_writer as fw

# POSIX-only markers, mirroring test_claude_json.py / test_trust.py: on Windows the
# advisory flock (fcntl) is absent so `_locked` is a documented best-effort no-op (no
# `.lock` sidecar), and Unix permission bits aren't honored (stat reports 0o666). Gate
# the assertions that check those two POSIX-specific behaviors; the leftover-file tests
# instead assert the *cross-platform* invariant (no stray .tmp/.staging/.trash), the
# same idiom test_claude_json.py uses (assert the bad file is absent, not exact
# directory contents).
needs_posix = pytest.mark.skipif(os.name != "posix", reason="POSIX file-mode semantics")


def _no_orphans(parent: Path) -> None:
    """Assert no stray temp/staging/trash artifacts linger in ``parent`` (any platform).

    The advisory-lock ``.lock`` sidecar is expected to linger on POSIX (same as the
    Foundation's ``claude_json._locked``) and is simply absent on Windows, so it is not
    checked here; only the transient write artifacts, which must NEVER be orphaned, are.
    """
    for child in parent.iterdir():
        name = child.name
        assert not name.endswith(".tmp"), f"orphaned temp file: {name}"
        assert ".staging-" not in name, f"orphaned staging dir: {name}"
        assert ".trash-" not in name, f"orphaned trash dir: {name}"


# --- path containment (reject escape before I/O) -----------------------------------


def test_resolve_contained_path_ok(tmp_path: Path) -> None:
    resolved = fw.resolve_contained_path(tmp_path, "sub/file.txt")
    assert resolved == (tmp_path / "sub" / "file.txt").resolve()
    assert not resolved.exists()  # resolving never creates anything


def test_resolve_contained_path_rejects_absolute(tmp_path: Path) -> None:
    with pytest.raises(fw.PathEscapeError):
        fw.resolve_contained_path(tmp_path, "/etc/passwd")


def test_resolve_contained_path_rejects_dotdot_traversal(tmp_path: Path) -> None:
    with pytest.raises(fw.PathEscapeError):
        fw.resolve_contained_path(tmp_path, "../escape")


def test_resolve_contained_path_rejects_dotdot_in_middle(tmp_path: Path) -> None:
    with pytest.raises(fw.PathEscapeError):
        fw.resolve_contained_path(tmp_path, "sub/../../escape")


def test_resolve_contained_path_rejects_empty_or_dot_components(tmp_path: Path) -> None:
    with pytest.raises(fw.PathEscapeError):
        fw.resolve_contained_path(tmp_path, ".")
    with pytest.raises(fw.PathEscapeError):
        fw.resolve_contained_path(tmp_path, "")


def test_resolve_contained_path_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "sub"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported here")
    with pytest.raises(fw.PathEscapeError):
        fw.resolve_contained_path(root, "sub/file.txt")


def test_resolve_contained_path_allows_root_itself(tmp_path: Path) -> None:
    # A single-component relative path landing exactly on an existing child of root
    # (not root itself) is fine; root == candidate only arises via a symlinked child
    # that resolves back to root, which is inherently still "contained".
    (tmp_path / "child").mkdir()
    resolved = fw.resolve_contained_path(tmp_path, "child")
    assert resolved == (tmp_path / "child").resolve()


# --- write_file: atomic create/replace ----------------------------------------------


def test_write_file_creates_new_file(tmp_path: Path) -> None:
    target = fw.write_file(tmp_path, "sub/dir/note.txt", "hello")
    assert target == (tmp_path / "sub" / "dir" / "note.txt").resolve()
    assert target.read_text(encoding="utf-8") == "hello"


def test_write_file_accepts_bytes(tmp_path: Path) -> None:
    fw.write_file(tmp_path, "note.bin", b"\x00\x01\x02")
    assert (tmp_path / "note.bin").read_bytes() == b"\x00\x01\x02"


def test_write_file_replaces_existing_content(tmp_path: Path) -> None:
    fw.write_file(tmp_path, "note.txt", "first")
    fw.write_file(tmp_path, "note.txt", "second")
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "second"


def test_write_file_rejects_path_escape_before_any_io(tmp_path: Path) -> None:
    with pytest.raises(fw.PathEscapeError):
        fw.write_file(tmp_path, "../escape.txt", "nope")
    assert not (tmp_path.parent / "escape.txt").exists()


def test_write_file_no_leftover_temp_file(tmp_path: Path) -> None:
    fw.write_file(tmp_path, "note.txt", "hello")
    # No stray .tmp is ever left (cross-platform); the .lock sidecar may linger on POSIX
    # and is absent on Windows, so _no_orphans deliberately ignores it.
    assert (tmp_path / "note.txt").exists()
    _no_orphans(tmp_path)


def test_write_file_verify_sees_current_bytes_and_can_abort(tmp_path: Path) -> None:
    # verify() runs inside the per-target lock right before the replace: it sees the
    # target's current bytes (None if absent) and its raise aborts the write entirely.
    seen: list = []

    def _record(current):
        seen.append(current)

    fw.write_file(tmp_path, "note.txt", "first", verify=_record)
    assert seen == [None]  # absent target → None
    fw.write_file(tmp_path, "note.txt", "second", verify=_record)
    assert seen[1] == b"first"  # existing target → its current bytes
    assert (tmp_path / "note.txt").read_text() == "second"


def test_write_file_verify_raise_aborts_write(tmp_path: Path) -> None:
    fw.write_file(tmp_path, "note.txt", "keep")

    def _boom(current):
        raise ValueError("stale")

    with pytest.raises(ValueError, match="stale"):
        fw.write_file(tmp_path, "note.txt", "overwrite", verify=_boom)
    assert (tmp_path / "note.txt").read_text() == "keep"  # unchanged
    _no_orphans(tmp_path)  # no temp left behind by the aborted write


@needs_posix
def test_write_file_new_file_gets_requested_mode(tmp_path: Path) -> None:
    fw.write_file(tmp_path, "secret.txt", "s", mode=0o600)
    mode = (tmp_path / "secret.txt").stat().st_mode & 0o777
    assert mode == 0o600


def test_write_file_replace_failure_cleans_up_temp_file(tmp_path: Path, monkeypatch) -> None:
    # If the final os.replace fails, the mkstemp'd temp file must not linger — the
    # exception still propagates (no silent swallow).
    real_replace = fw.os.replace

    def boom(src, dst):
        raise OSError("simulated: replace failed")

    monkeypatch.setattr(fw.os, "replace", boom)
    with pytest.raises(OSError, match="simulated"):
        fw.write_file(tmp_path, "note.txt", "hello")
    monkeypatch.setattr(fw.os, "replace", real_replace)
    names = {p.name for p in tmp_path.iterdir()}
    assert not any(name.endswith(".tmp") for name in names)  # temp file cleaned up
    assert "note.txt" not in names  # nothing was ever promoted


@needs_posix
def test_write_file_replace_preserves_existing_mode(tmp_path: Path) -> None:
    fw.write_file(tmp_path, "note.txt", "first", mode=0o600)
    (tmp_path / "note.txt").chmod(0o640)
    fw.write_file(tmp_path, "note.txt", "second", mode=0o600)
    mode = (tmp_path / "note.txt").stat().st_mode & 0o777
    assert mode == 0o640  # existing mode preserved, not reset to the `mode` default


def test_write_file_non_posix_skips_chmod(tmp_path: Path, monkeypatch) -> None:
    # On Windows file permissions are ACL-based, so the POSIX mode-preservation branch
    # is skipped; the write still completes. Patch the _is_posix seam (not os.name,
    # which would break tempfile), mirroring claude_json's equivalent test.
    monkeypatch.setattr(fw, "_is_posix", lambda: False)
    fw.write_file(tmp_path, "note.txt", "hello")
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "hello"


# --- read_file: redacted read path ---------------------------------------------------


def test_read_file_round_trips_plain_content(tmp_path: Path) -> None:
    fw.write_file(tmp_path, "note.txt", "hello world")
    assert fw.read_file(tmp_path, "note.txt") == "hello world"


def test_read_file_preserves_line_endings_byte_exactly(tmp_path: Path) -> None:
    # read_file reads bytes-then-decodes (NOT read_text), so CRLF/mixed line endings
    # survive verbatim on every OS — read_text would collapse \r\n -> \n on read and
    # mutate a consumer's content (a skill script, a subagent prompt, a CLAUDE.md).
    payload = "line1\r\nline2\nline3\r\n"
    fw.write_file(tmp_path, "crlf.txt", payload)
    assert fw.read_file(tmp_path, "crlf.txt") == payload


def test_read_file_redacts_secret_shaped_lines(tmp_path: Path) -> None:
    fw.write_file(tmp_path, "config.env", "API_TOKEN=sk-live-deadbeef\nHOST=localhost\n")
    text = fw.read_file(tmp_path, "config.env")
    assert "sk-live-deadbeef" not in text
    assert "HOST=localhost" in text


def test_read_file_missing_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        fw.read_file(tmp_path, "absent.txt")


def test_read_file_rejects_path_escape(tmp_path: Path) -> None:
    with pytest.raises(fw.PathEscapeError):
        fw.read_file(tmp_path, "../escape.txt")


# --- replace_tree: atomic dir create/replace -----------------------------------------


def _populate(files: dict[str, str]):
    def _build(staging: Path) -> None:
        for rel, content in files.items():
            p = staging / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

    return _build


def test_replace_tree_creates_new_directory(tmp_path: Path) -> None:
    target = fw.replace_tree(tmp_path, "skill", _populate({"SKILL.md": "# hi", "lib/a.py": "x"}))
    assert target == (tmp_path / "skill").resolve()
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "# hi"
    assert (target / "lib" / "a.py").read_text(encoding="utf-8") == "x"


def test_replace_tree_no_staging_leftover_on_success(tmp_path: Path) -> None:
    fw.replace_tree(tmp_path, "skill", _populate({"SKILL.md": "# hi"}))
    assert (tmp_path / "skill").is_dir()
    _no_orphans(tmp_path)  # no .staging-* left behind (cross-platform)


def test_replace_tree_replaces_existing_directory_wholesale(tmp_path: Path) -> None:
    fw.replace_tree(tmp_path, "skill", _populate({"SKILL.md": "old", "gone.txt": "x"}))
    fw.replace_tree(tmp_path, "skill", _populate({"SKILL.md": "new"}))
    target = tmp_path / "skill"
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "new"
    assert not (target / "gone.txt").exists()  # old tree fully replaced, not merged


def test_replace_tree_no_trash_leftover_after_replace(tmp_path: Path) -> None:
    fw.replace_tree(tmp_path, "skill", _populate({"SKILL.md": "old"}))
    fw.replace_tree(tmp_path, "skill", _populate({"SKILL.md": "new"}))
    assert (tmp_path / "skill").is_dir()
    _no_orphans(tmp_path)  # no .trash-* / .staging-* left behind (cross-platform)


def test_replace_tree_build_failure_leaves_target_untouched(tmp_path: Path) -> None:
    fw.replace_tree(tmp_path, "skill", _populate({"SKILL.md": "original"}))

    def _boom(staging: Path) -> None:
        (staging / "partial.txt").write_text("x", encoding="utf-8")
        raise RuntimeError("build failed")

    with pytest.raises(RuntimeError, match="build failed"):
        fw.replace_tree(tmp_path, "skill", _boom)

    target = tmp_path / "skill"
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "original"  # unchanged
    _no_orphans(tmp_path)  # no half-built .staging-* left behind (cross-platform)


def test_replace_tree_build_failure_on_create_leaves_no_target(tmp_path: Path) -> None:
    def _boom(staging: Path) -> None:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError, match="nope"):
        fw.replace_tree(tmp_path, "skill", _boom)
    assert not (tmp_path / "skill").exists()
    assert list(tmp_path.iterdir()) == []  # staging dir cleaned up


def test_replace_tree_promote_failure_restores_displaced_tree(tmp_path: Path, monkeypatch) -> None:
    # Greptile P1: if the second (promote) rename fails after the first (swap-out) rename
    # already succeeded, the displaced original tree must be restored — the target path is
    # never left permanently missing — the un-promoted staging dir must NOT be orphaned,
    # and the failure still propagates.
    fw.replace_tree(tmp_path, "skill", _populate({"SKILL.md": "original"}))

    real_replace = fw.os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # the staging -> target promote call
            raise OSError("simulated: promote failed")
        return real_replace(src, dst)

    monkeypatch.setattr(fw.os, "replace", flaky_replace)
    with pytest.raises(OSError, match="promote failed"):
        fw.replace_tree(tmp_path, "skill", _populate({"SKILL.md": "new"}))
    monkeypatch.setattr(fw.os, "replace", real_replace)

    target = tmp_path / "skill"
    assert target.exists()  # never left missing
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "original"  # restored
    _no_orphans(tmp_path)  # P1: staging dir removed, trash restored — no .staging-*/.trash-*


def test_replace_tree_create_promote_failure_removes_staging(tmp_path: Path, monkeypatch) -> None:
    # Greptile P1, create branch: when the single create-path rename fails (no existing
    # target to displace), the un-promoted staging dir must be removed, never orphaned,
    # and the failure still propagates. monkeypatch auto-restores os.replace at teardown.
    def boom(src, dst):
        raise OSError("simulated: create-promote failed")

    monkeypatch.setattr(fw.os, "replace", boom)
    with pytest.raises(OSError, match="create-promote failed"):
        fw.replace_tree(tmp_path, "skill", _populate({"SKILL.md": "new"}))

    assert not (tmp_path / "skill").exists()  # nothing promoted
    _no_orphans(tmp_path)  # P1: no orphaned .staging-*


def test_replace_tree_rejects_path_escape_before_building(tmp_path: Path) -> None:
    called = False

    def _build(staging: Path) -> None:
        nonlocal called
        called = True

    with pytest.raises(fw.PathEscapeError):
        fw.replace_tree(tmp_path, "../escape", _build)
    assert called is False  # never even started building


# --- delete_path: idempotent file/tree delete ----------------------------------------


def test_delete_path_removes_file(tmp_path: Path) -> None:
    fw.write_file(tmp_path, "note.txt", "x")
    assert fw.delete_path(tmp_path, "note.txt") is True
    assert not (tmp_path / "note.txt").exists()


def test_delete_path_removes_directory_tree(tmp_path: Path) -> None:
    fw.replace_tree(tmp_path, "skill", _populate({"SKILL.md": "x", "lib/a.py": "y"}))
    assert fw.delete_path(tmp_path, "skill") is True
    assert not (tmp_path / "skill").exists()


def test_delete_path_missing_is_idempotent_false(tmp_path: Path) -> None:
    assert fw.delete_path(tmp_path, "absent") is False


def test_delete_path_rejects_path_escape(tmp_path: Path) -> None:
    with pytest.raises(fw.PathEscapeError):
        fw.delete_path(tmp_path, "../escape")


def test_delete_path_no_trash_leftover(tmp_path: Path) -> None:
    fw.replace_tree(tmp_path, "skill", _populate({"SKILL.md": "x"}))
    fw.delete_path(tmp_path, "skill")
    # The target directory is gone; no `.trash-*` lingers (it is always cleaned up after
    # the swap). The `.lock` sidecar may linger on POSIX / is absent on Windows, so
    # _no_orphans deliberately ignores it.
    assert not (tmp_path / "skill").exists()
    _no_orphans(tmp_path)


# --- flock: serializes concurrent writers on the SAME target -----------------------


@pytest.mark.skipif(fw.fcntl is None, reason="advisory flock is POSIX-only (no fcntl)")
def test_locked_serializes_concurrent_file_writers(tmp_path: Path, monkeypatch) -> None:
    # Same shape as test_trust.py's flock regression test: slow down the guarded
    # section and assert the in-flight count of concurrent writers never exceeds 1.
    counter_lock = threading.Lock()
    active = 0
    max_active = 0
    real_write_bytes = fw.os.fdopen

    def slow_fdopen(fd, *args, **kwargs):
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.03)
            return real_write_bytes(fd, *args, **kwargs)
        finally:
            with counter_lock:
                active -= 1

    monkeypatch.setattr(fw.os, "fdopen", slow_fdopen)

    threads = [
        threading.Thread(target=fw.write_file, args=(tmp_path, "shared.txt", f"v{i}"))
        for i in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert max_active == 1  # flock fully serialized the writers
    assert (tmp_path / "shared.txt").exists()


def test_locked_noop_without_fcntl(tmp_path: Path, monkeypatch) -> None:
    # On a platform without fcntl (Windows) the lock degrades to a no-op: the write
    # still completes and no .lock sidecar is created.
    monkeypatch.setattr(fw, "fcntl", None)
    fw.write_file(tmp_path, "note.txt", "hello")
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "hello"
    assert not (tmp_path / "note.txt.lock").exists()


def test_locked_lockfile_open_failure_is_best_effort(tmp_path: Path, monkeypatch) -> None:
    # If the .lock sidecar can't be opened, never block the write — proceed unlocked
    # (the atomic replace still protects the file).
    real_open = fw.os.open

    def boom(path, *args, **kwargs):
        if str(path).endswith(".lock"):
            raise OSError("simulated: cannot open lock file")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(fw.os, "open", boom)
    fw.write_file(tmp_path, "note.txt", "hello")
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "hello"
