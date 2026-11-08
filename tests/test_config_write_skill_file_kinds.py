"""Skill members that are not plain readable files, kept modes, and the flow ``?`` spelling.

TASK-500: a member that a save names keeps the stored file's mode on POSIX (setuid and
setgid cleared), the stored-tree walk refuses a tree over :data:`MAX_STORED_ENTRIES`, a
regular file at ``skills/<name>`` is a 422, and ``SKILL.md`` is read without blocking on a
FIFO. TASK-531: reading a member that is a directory, a FIFO, a device or unreadable is a
named 422, never a 500 and never a blocked thread. TASK-530: a ``?`` inside a flow
collection, which YAML 1.1 and the YAML 1.2 that Claude Code reads split, is refused with
its own message, and a resent wholly masked header is refused with that real cause.
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster import config_write as cw
from clauster import config_write_skills as sk
from clauster.app import create_app
from clauster.config import load_config
from conftest import needs_symlink

MASK = cw.REDACTION_SENTINEL
FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"
_VALID_MD = "---\ndescription: does a thing\n---\nInstructions here.\n"
_RUN_SH = b"#!/bin/sh\necho run\n"
_POSIX_MODES = pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
_NON_ROOT = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="needs POSIX mode bits and a non-root user"
)
_FIFO = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo (POSIX)")


def _store(tmp_path: Path) -> Path:
    """Store skill ``s`` by hand: SKILL.md and an executable script."""
    skill_dir = tmp_path / "skills" / "s"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / sk.SKILL_FILENAME).write_bytes(_VALID_MD.encode())
    (skill_dir / "scripts" / "run.sh").write_bytes(_RUN_SH)
    return skill_dir


def _save(tmp_path: Path, files: dict[str, str], **kwargs: object) -> None:
    kwargs.setdefault("expected_hash", cw.hash_bytes(_VALID_MD.encode()))
    kwargs.setdefault("confirm_scripts", sk.SCRIPT_CONFIRM_TOKEN)
    sk.write_skill(tmp_path, "s", files, **kwargs)  # type: ignore[arg-type]


def _no_orphans(skills_root: Path) -> None:
    assert [p.name for p in skills_root.iterdir() if p.name.startswith(".")] == []


def _returns_within(seconds: float, fifo: Path, call):
    """Run ``call()`` in a thread; fail (and unblock it) if it does not return in time.

    A read that opens the FIFO would block until a writer opens it, so on a timeout this
    opens the FIFO for writing once, which lets the blocked open return and the test end.
    """
    box: dict[str, object] = {}

    def _run() -> None:
        try:
            box["value"] = call()
        except BaseException as exc:  # handed back to the test thread below
            box["error"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        worker.join(seconds)
        pytest.fail(f"the call blocked on the FIFO {fifo.name}")
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box.get("value")


# --- TASK-500: a named member keeps the stored mode ------------------------------------


@_POSIX_MODES
@pytest.mark.parametrize(
    ("stored", "kept"),
    [(0o755, 0o755), (0o700, 0o700), (0o640, 0o640), (0o4755, 0o755), (0o2750, 0o750)],
)
def test_a_saved_member_keeps_its_stored_mode_without_set_id_bits(
    tmp_path: Path, stored: int, kept: int
) -> None:
    skill_dir = _store(tmp_path)
    run_sh = skill_dir / "scripts" / "run.sh"
    run_sh.chmod(stored)
    _save(tmp_path, {sk.SKILL_FILENAME: _VALID_MD, "scripts/run.sh": "echo new\n"})
    assert run_sh.read_bytes() == b"echo new\n"
    assert run_sh.stat().st_mode & 0o7777 == kept


@_POSIX_MODES
def test_a_saved_member_under_another_key_spelling_keeps_its_mode(tmp_path: Path) -> None:
    skill_dir = _store(tmp_path)
    (skill_dir / "scripts" / "run.sh").chmod(0o750)
    _save(tmp_path, {sk.SKILL_FILENAME: _VALID_MD, "scripts//run.sh": "echo new\n"})
    assert (skill_dir / "scripts" / "run.sh").stat().st_mode & 0o7777 == 0o750


@_POSIX_MODES
def test_a_new_member_gets_the_default_mode(tmp_path: Path) -> None:
    skill_dir = _store(tmp_path)
    (skill_dir / "scripts" / "run.sh").chmod(0o700)
    probe = tmp_path / "probe"
    probe.touch()  # the default a new file gets here: 0o666 less the umask
    _save(tmp_path, {sk.SKILL_FILENAME: _VALID_MD, "scripts/new.sh": "echo new\n"})
    assert (skill_dir / "scripts" / "new.sh").stat().st_mode & 0o7777 == (
        probe.stat().st_mode & 0o7777
    )
    assert (skill_dir / "scripts" / "run.sh").stat().st_mode & 0o7777 == 0o700  # kept


@_POSIX_MODES
def test_skill_md_keeps_its_stored_mode_on_a_save(tmp_path: Path) -> None:
    skill_dir = _store(tmp_path)
    (skill_dir / sk.SKILL_FILENAME).chmod(0o600)
    _save(tmp_path, {sk.SKILL_FILENAME: "---\ndescription: new\n---\nNew.\n"})
    assert (skill_dir / sk.SKILL_FILENAME).stat().st_mode & 0o7777 == 0o600


# --- TASK-500: the walk refuses a tree over the entry cap -------------------------------


def test_the_walk_refuses_a_tree_over_the_entry_cap(tmp_path: Path, monkeypatch) -> None:
    skill_dir = _store(tmp_path)  # SKILL.md, scripts, scripts/run.sh: three entries
    (skill_dir / "refs").mkdir()
    (skill_dir / "refs" / "a.md").write_bytes(b"")  # five entries, no bytes
    monkeypatch.setattr(sk, "MAX_STORED_ENTRIES", 4)
    with pytest.raises(cw.InvalidCandidateError, match="holds more than 4 stored files"):
        _save(tmp_path, {sk.SKILL_FILENAME: "---\ndescription: new\n---\n"})
    assert (skill_dir / sk.SKILL_FILENAME).read_bytes() == _VALID_MD.encode()
    assert (skill_dir / "refs" / "a.md").exists()
    _no_orphans(tmp_path / "skills")
    monkeypatch.setattr(sk, "MAX_STORED_ENTRIES", 5)  # exactly at the cap saves
    _save(tmp_path, {sk.SKILL_FILENAME: "---\ndescription: new\n---\n"})
    assert (skill_dir / "refs" / "a.md").exists()


def test_the_walk_stops_listing_a_huge_directory_at_the_cap(tmp_path: Path, monkeypatch) -> None:
    skill_dir = _store(tmp_path)
    for index in range(40):
        (skill_dir / f"f{index}").write_bytes(b"")
    monkeypatch.setattr(sk, "MAX_STORED_ENTRIES", 3)
    real = sk._list_stored_dir
    lengths: list[int] = []

    def _recording(path, expected, member, **kwargs):
        out = real(path, expected, member, **kwargs)
        lengths.append(len(out))
        return out

    monkeypatch.setattr(sk, "_list_stored_dir", _recording)
    with pytest.raises(cw.InvalidCandidateError, match="holds more than 3"):
        _save(tmp_path, {sk.SKILL_FILENAME: _VALID_MD})
    assert lengths == [4]  # the listing stopped one past the cap, not at 42 entries


# --- TASK-500 and TASK-531: entries that are not a plain readable file ------------------


def test_a_regular_file_at_the_skill_name_is_refused_on_save_and_read(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "s").write_bytes(b"not a skill\n")
    for expected_hash in (None, cw.hash_bytes(b"")):
        with pytest.raises(cw.InvalidCandidateError, match="'s' is not a directory"):
            sk.write_skill(
                tmp_path, "s", {sk.SKILL_FILENAME: _VALID_MD}, expected_hash=expected_hash
            )
    with pytest.raises(cw.InvalidCandidateError, match="'s' is not a directory"):
        sk._skill_md_hash(tmp_path / "skills", "s")
    with pytest.raises(cw.InvalidCandidateError, match="'s' is not a directory"):
        sk.read_skill_file(tmp_path, "s")
    assert (tmp_path / "skills" / "s").read_bytes() == b"not a skill\n"
    _no_orphans(tmp_path / "skills")


def test_a_skill_md_that_is_a_directory_is_refused_on_save_and_read(tmp_path: Path) -> None:
    (tmp_path / "skills" / "s" / sk.SKILL_FILENAME).mkdir(parents=True)
    with pytest.raises(cw.InvalidCandidateError, match="'SKILL.md' is a directory"):
        sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: _VALID_MD}, expected_hash=None)
    with pytest.raises(cw.InvalidCandidateError, match="'SKILL.md' is a directory"):
        sk.read_skill_file(tmp_path, "s")
    assert (tmp_path / "skills" / "s" / sk.SKILL_FILENAME).is_dir()


@_FIFO
def test_a_fifo_at_skill_md_never_blocks_a_save(tmp_path: Path) -> None:
    fifo = tmp_path / "skills" / "s" / sk.SKILL_FILENAME
    fifo.parent.mkdir(parents=True)
    os.mkfifo(fifo)
    with pytest.raises(cw.InvalidCandidateError, match="'SKILL.md' is a symlink or not a regular"):
        _returns_within(
            5,
            fifo,
            lambda: sk.write_skill(
                tmp_path, "s", {sk.SKILL_FILENAME: _VALID_MD}, expected_hash=None
            ),
        )
    assert stat.S_ISFIFO(os.lstat(fifo).st_mode)
    _no_orphans(tmp_path / "skills")


@_FIFO
def test_a_fifo_at_skill_md_never_blocks_the_check_inside_the_lock(
    tmp_path: Path, monkeypatch
) -> None:
    # SKILL.md becomes a FIFO between the check outside the lock and the re-check inside.
    skill_dir = _store(tmp_path)
    real_replace_tree = sk.fw.replace_tree

    def _racing_replace_tree(root, relative, build):
        (skill_dir / sk.SKILL_FILENAME).unlink()
        os.mkfifo(skill_dir / sk.SKILL_FILENAME)
        return real_replace_tree(root, relative, build)

    monkeypatch.setattr(sk.fw, "replace_tree", _racing_replace_tree)
    with pytest.raises(cw.InvalidCandidateError, match="'SKILL.md' is a symlink or not a regular"):
        _returns_within(
            5,
            skill_dir / sk.SKILL_FILENAME,
            lambda: _save(tmp_path, {sk.SKILL_FILENAME: "---\ndescription: new\n---\n"}),
        )
    assert (skill_dir / "scripts" / "run.sh").read_bytes() == _RUN_SH
    _no_orphans(tmp_path / "skills")


@_FIFO
def test_the_skill_list_never_blocks_on_a_skill_md_swapped_for_a_fifo(
    tmp_path: Path, monkeypatch
) -> None:
    # SKILL.md becomes a FIFO between the listing's is_file() check and its read.
    fifo = tmp_path / "skills" / "s" / sk.SKILL_FILENAME
    fifo.parent.mkdir(parents=True)
    os.mkfifo(fifo)
    real_is_file = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda self: self == fifo or real_is_file(self))
    listed = _returns_within(5, fifo, lambda: sk.list_skills(tmp_path))
    assert "is a symlink or not a regular file" in listed[0]["frontmatter_error"]


@needs_symlink
def test_a_symlinked_skill_md_is_refused_before_the_save_reads_it(tmp_path: Path) -> None:
    skill_dir = _store(tmp_path)
    (skill_dir / "real.md").write_bytes(_VALID_MD.encode())
    (skill_dir / sk.SKILL_FILENAME).unlink()
    (skill_dir / sk.SKILL_FILENAME).symlink_to("real.md")
    with pytest.raises(cw.InvalidCandidateError, match="'SKILL.md' is a symlink or not a regular"):
        _save(tmp_path, {sk.SKILL_FILENAME: _VALID_MD})
    assert (skill_dir / sk.SKILL_FILENAME).is_symlink()


@_NON_ROOT
def test_an_unreadable_skill_md_is_a_named_refusal_on_save_and_read(tmp_path: Path) -> None:
    skill_dir = _store(tmp_path)
    (skill_dir / sk.SKILL_FILENAME).chmod(0o000)
    try:
        with pytest.raises(cw.InvalidCandidateError, match="'SKILL.md' could not be read"):
            _save(tmp_path, {sk.SKILL_FILENAME: _VALID_MD})
        with pytest.raises(cw.InvalidCandidateError, match="'SKILL.md' could not be read"):
            sk.read_skill_file(tmp_path, "s")
    finally:
        (skill_dir / sk.SKILL_FILENAME).chmod(0o644)


@pytest.mark.parametrize("relative", ["scripts", "scripts/"])
def test_reading_a_directory_member_is_a_named_refusal(tmp_path: Path, relative: str) -> None:
    _store(tmp_path)
    with pytest.raises(cw.InvalidCandidateError, match="'scripts/?' is a directory in the skill"):
        sk.read_skill_file_for_edit(tmp_path, "s", relative)


@_FIFO
def test_reading_a_fifo_member_never_opens_it(tmp_path: Path) -> None:
    skill_dir = _store(tmp_path)
    fifo = skill_dir / "scripts" / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(cw.InvalidCandidateError, match="'scripts/pipe' is a symlink or not a"):
        _returns_within(
            5, fifo, lambda: sk.read_skill_file_for_edit(tmp_path, "s", "scripts/pipe")
        )


def test_reading_a_device_member_is_a_named_refusal(tmp_path: Path, monkeypatch) -> None:
    # A device node needs root to make, so the lstat answers as it would for one.
    skill_dir = _store(tmp_path)
    device = (skill_dir / "scripts" / "run.sh").resolve()
    real_lstat = sk.os.lstat

    def _lstat(path, *args, **kwargs):
        got = real_lstat(path, *args, **kwargs)
        if Path(path) == device:
            fields = list(got)[:10]
            fields[0] = stat.S_IFCHR | 0o600
            return os.stat_result(fields)
        return got

    opened: list[object] = []
    real_open = sk.os.open
    monkeypatch.setattr(sk.os, "lstat", _lstat)
    monkeypatch.setattr(sk.os, "open", lambda *a, **k: opened.append(a[0]) or real_open(*a, **k))
    with pytest.raises(cw.InvalidCandidateError, match="'scripts/run.sh' is a symlink or not a"):
        sk.read_skill_file_for_edit(tmp_path, "s", "scripts/run.sh")
    assert opened == []  # refused on the lstat, before any open


@_NON_ROOT
@pytest.mark.parametrize("unreadable", ["scripts/run.sh", "scripts"])
def test_reading_an_unreadable_member_is_a_named_refusal(tmp_path: Path, unreadable: str) -> None:
    skill_dir = _store(tmp_path)
    (skill_dir / unreadable).chmod(0o000)
    try:
        with pytest.raises(cw.InvalidCandidateError, match="could not be read"):
            sk.read_skill_file_for_edit(tmp_path, "s", "scripts/run.sh")
    finally:
        (skill_dir / unreadable).chmod(0o755)


def test_reading_a_member_swapped_after_the_lstat_is_refused(tmp_path: Path, monkeypatch) -> None:
    skill_dir = _store(tmp_path)
    (skill_dir / "other.sh").write_bytes(b"other\n")
    other = os.stat(skill_dir / "other.sh")
    real_fstat = sk.os.fstat
    monkeypatch.setattr(sk.os, "fstat", lambda fd: other if fd >= 0 else real_fstat(fd))
    with pytest.raises(cw.InvalidCandidateError, match="'scripts/run.sh' changed while it was"):
        sk.read_skill_file_for_edit(tmp_path, "s", "scripts/run.sh")


def test_reading_a_member_under_a_file_reads_as_absent(tmp_path: Path) -> None:
    _store(tmp_path)
    content, _hash, exists, _note = sk.read_skill_file_for_edit(tmp_path, "s", "scripts/run.sh/x")
    assert (content, exists) == ("", False)


_ROUTE_CFG = "config_write:\n  enabled: true\n"


def _route_client(write_config, tmp_path: Path) -> TestClient:
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{_ROUTE_CFG}")
    return TestClient(create_app(load_config(cfg)))


def test_the_routes_answer_422_for_a_directory_member_and_a_file_skill(
    write_config, tmp_path, projects_root
) -> None:
    skills = projects_root / "alpha" / ".claude" / "skills"
    _store(projects_root / "alpha" / ".claude")
    (skills / "flat").write_bytes(b"x\n")
    with _route_client(write_config, tmp_path) as c:
        directory = c.get(
            "/api/config-write/skills/file",
            params={"project": "alpha", "name": "s", "relative": "scripts"},
        )
        flat_read = c.get(
            "/api/config-write/skills/file", params={"project": "alpha", "name": "flat"}
        )
        flat_write = c.put(
            "/api/config-write/skills",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "name": "flat",
                "files": {sk.SKILL_FILENAME: _VALID_MD},
            },
        )
    assert directory.status_code == 422, directory.text
    assert "is a directory in the skill" in directory.json()["detail"]
    assert flat_read.status_code == 422, flat_read.text
    assert flat_write.status_code == 422, flat_write.text
    assert "is not a directory" in flat_write.json()["detail"]


# --- TASK-530: a `?` in a flow collection -----------------------------------------------

_URL_FLOW = "---\ndescription: d\nmcpServers: {srv: {url: https://h.example/p?k=qv-q-one}}\n---\n"


@pytest.mark.parametrize(
    "header",
    [
        "mcpServers: {srv: {url: https://h.example/p?k=v}}",  # PyYAML: not valid YAML
        "mcpServers: {srv: {type: http, url: https://h.example/p?k=v, x: y}}",
        "mcpServers: {srv: {args: [a, ?b]}}",  # loads in both, as a key and as text
        "x: {?a: b}",
        "x: [a ? b]",
        "x: [a,\n  ?b]",
    ],
)
def test_a_question_mark_the_two_yaml_versions_split_is_refused(header: str) -> None:
    with pytest.raises(cw.InvalidCandidateError, match=r"has a '\?' at line \d+, column \d+"):
        sk.validate_skill_md_content(f"---\ndescription: d\n{header}\n---\nBody.\n")


def test_the_refusal_names_the_line_and_column_in_the_file() -> None:
    with pytest.raises(cw.InvalidCandidateError, match="at line 3, column 44 inside a '{...}'"):
        sk.validate_skill_md_content(_URL_FLOW)


@pytest.mark.parametrize(
    "header",
    [
        'mcpServers: {srv: {url: "https://h.example/p?k=v"}}',  # quoted, as the message asks
        "mcpServers:\n  srv:\n    url: https://h.example/p?k=v",  # block style
        "x: [? a, ? b]",  # an explicit key both read alike
        "x: {? a : b}",
        "x: [a, ?]",
        "argument-hint: what? why?",  # a plain value outside any flow collection
    ],
)
def test_a_question_mark_both_versions_read_alike_is_accepted(header: str) -> None:
    sk.validate_skill_md_content(f"---\ndescription: d\n{header}\n---\nBody.\n")


def test_the_subagent_surface_shares_the_refusal() -> None:
    with pytest.raises(
        cw.InvalidCandidateError, match=r"frontmatter has a '\?' at line 3, column 6"
    ):
        cw.load_frontmatter_yaml("name: a\nx: [a?b]", what="frontmatter", line_offset=1)


def test_a_flow_question_mark_header_is_masked_whole(tmp_path: Path) -> None:
    # PyYAML reads `?qv-arg-one` as a key, so the masked-value walk would not see it as an
    # `args` value, which is what Claude Code reads.
    stored = "---\ndescription: d\nmcpServers: {srv: {args: [?qv-arg-one]}}\n---\nRun it.\n"
    path = tmp_path / "skills" / "s" / sk.SKILL_FILENAME
    path.parent.mkdir(parents=True)
    path.write_bytes(stored.encode())
    content, _hash, _exists = sk.read_skill_file(tmp_path, "s")
    assert "qv-arg-one" not in content
    assert content == f"---\n{MASK}\n{MASK}\n---\nRun it.\n"


def test_a_resent_wholly_masked_header_is_refused_with_its_real_cause(tmp_path: Path) -> None:
    path = tmp_path / "skills" / "s" / sk.SKILL_FILENAME
    path.parent.mkdir(parents=True)
    stored = _URL_FLOW + "Run it.\n"
    path.write_bytes(stored.encode())
    content, file_hash, _exists, _note = sk.read_skill_file_for_edit(tmp_path, "s")
    assert content == f"---\n{MASK}\n{MASK}\n---\nRun it.\n"  # masked whole
    listed = sk.list_skills(tmp_path)[0]["frontmatter_error"]
    with pytest.raises(cw.InvalidCandidateError) as caught:
        sk.write_skill(
            tmp_path, "s", {sk.SKILL_FILENAME: content + "More.\n"}, expected_hash=file_hash
        )
    message = str(caught.value)
    assert "'hooks' or 'mcpServers' changed" not in message
    assert "the stored file itself does not pass the checks" in message
    assert "has a '?' at line 3, column 44" in message
    assert listed in message  # the cause the skill list already shows, nothing more
    assert "qv-q-one" not in message
    assert path.read_bytes() == stored.encode()


def test_an_edited_masked_header_is_still_blamed_on_the_sent_text(tmp_path: Path) -> None:
    # The operator's own broken line is the cause when the stored header passes.
    path = tmp_path / "skills" / "s" / sk.SKILL_FILENAME
    path.parent.mkdir(parents=True)
    stored = "---\ndescription: d\napi_token: qv-t-one\n---\nRun it.\n"
    path.write_bytes(stored.encode())
    content, file_hash, _exists, _note = sk.read_skill_file_for_edit(tmp_path, "s")
    assert f"api_token: {MASK}\n" in content
    broken = content.replace("description: d\n", 'description: "d\n')
    with pytest.raises(cw.InvalidCandidateError) as caught:
        sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: broken}, expected_hash=file_hash)
    assert "stored file itself" not in str(caught.value)
    assert path.read_bytes() == stored.encode()


@_NON_ROOT
def test_an_unsearchable_skills_root_is_a_named_refusal(tmp_path: Path) -> None:
    _store(tmp_path)
    (tmp_path / "skills").chmod(0o000)
    try:
        with pytest.raises(cw.InvalidCandidateError, match="skill 's' could not be read"):
            sk.read_skill_file(tmp_path, "s")
    finally:
        (tmp_path / "skills").chmod(0o755)


def test_the_skill_list_reports_a_skill_md_gone_since_its_check(
    tmp_path: Path, monkeypatch
) -> None:
    skill_md = _store(tmp_path) / sk.SKILL_FILENAME
    real_lstat = sk.os.lstat

    def _gone(path, *args, **kwargs):
        if Path(path) == skill_md:
            raise FileNotFoundError(2, "No such file or directory")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(sk.os, "lstat", _gone)
    listed = sk.list_skills(tmp_path)
    assert (
        listed[0]["frontmatter_error"] == "SKILL.md could not be read: No such file or directory"
    )
