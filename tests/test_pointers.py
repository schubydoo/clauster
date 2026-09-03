from __future__ import annotations

import json
from pathlib import Path

import pytest

from clauster import pointers


def test_sanitize_cwd_replaces_all_non_alphanumerics():
    # Verified scheme: /mnt/nas/projects/unnamed_project -> -mnt-nas-projects-unnamed-project
    assert (
        pointers.sanitize_cwd(Path("/mnt/nas/projects/clauster")) == "-mnt-nas-projects-clauster"
    )
    assert (
        pointers.sanitize_cwd(Path("/mnt/nas/projects/unnamed_project"))
        == "-mnt-nas-projects-unnamed-project"
    )


def test_pointer_path_for_uses_sanitized_dir(tmp_path: Path):
    p = pointers.pointer_path_for(Path("/mnt/nas/projects/clauster"), tmp_path)
    assert p == tmp_path / "-mnt-nas-projects-clauster" / "bridge-pointer.json"


def test_load_pointer_parses_fixture(fixtures_dir: Path):
    ptr = pointers.load_pointer(fixtures_dir / "pointers" / "test1.bridge-pointer.json")
    assert ptr is not None
    assert ptr.environment_id == "env_01RHE7cHW3DawXjGRp5Ae3va"
    assert ptr.session_id == "session_01LG15p2JVjwBamscENjuBLi"
    assert ptr.pid == 81750
    assert ptr.proc_start == "2590192"


def test_load_pointer_missing_returns_none(tmp_path: Path):
    assert pointers.load_pointer(tmp_path / "nope.json") is None


def test_load_pointer_malformed_returns_none(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert pointers.load_pointer(bad) is None


def test_load_pointer_non_utf8_returns_none(tmp_path: Path):
    # A non-UTF-8 file raises UnicodeDecodeError (a ValueError) on read; the
    # malformed -> None contract must still hold (not bubble the exception).
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"\xff\xfe\x00not utf-8")
    assert pointers.load_pointer(bad) is None


def test_load_pointer_deeply_nested_json_returns_none(tmp_path: Path):
    # Deeply-nested JSON overflows CPython's recursive scanner, which raises RecursionError
    # on every supported interpreter (the message changed from "maximum recursion depth
    # exceeded" on <=3.13 to "Stack overflow" on 3.14+, but not the type). RecursionError
    # is not a ValueError, so without this arm it would escape the handler and break the
    # malformed -> None contract, propagating to whatever surface called it.
    bad = tmp_path / "nested.json"
    bad.write_text("[" * 100_000)
    assert pointers.load_pointer(bad) is None


def test_load_pointer_oversized_int_literal_returns_none(tmp_path: Path):
    # The base-10 integer-string-conversion limit (CVE-2020-10735) makes json.loads
    # raise a bare ValueError -- not a JSONDecodeError -- for a >4300-digit int.
    payload = "1" * 5000
    # Pin the precondition: the limit is settable at runtime (sys.set_int_max_str_digits,
    # PYTHONINTMAXSTRDIGITS), and with it disabled json.loads would return an int and the
    # assertion below would pass through model_validate instead -- green for the wrong
    # reason. Proving the arm is reachable is what makes this a regression test.
    with pytest.raises(ValueError) as raised:
        json.loads(payload)
    assert not isinstance(raised.value, json.JSONDecodeError)

    bad = tmp_path / "bigint.json"
    bad.write_text(payload)
    assert pointers.load_pointer(bad) is None


def test_load_pointer_valid_json_wrong_shape_returns_none(tmp_path: Path):
    # Parseable JSON whose shape doesn't satisfy BridgePointer (missing required
    # fields) raises a pydantic ValidationError (a ValueError) on model_validate;
    # the malformed -> None contract must still hold.

    bad = tmp_path / "wrong_shape.json"
    bad.write_text(json.dumps({"sessionId": "s", "unexpected": True}))
    assert pointers.load_pointer(bad) is None


def test_pointer_for_project_forward_resolves_and_loads(fixtures_dir: Path):
    # pointer_for_project maps a project path forward to its sanitized dir under
    # claude_projects_dir; with no pointer there it returns None (not an error).
    assert pointers.pointer_for_project(Path("/no/such/project"), fixtures_dir) is None


def test_fixture_pointers_are_not_live(fixtures_dir: Path):
    # The captured pointers reference long-dead PIDs -> never trusted as live.
    for name in ("test1", "test2", "dockerize2"):
        ptr = pointers.load_pointer(fixtures_dir / "pointers" / f"{name}.bridge-pointer.json")
        assert ptr is not None
        assert pointers.is_live(ptr) is False


# ----- clear_pointer (#867 L1) --------------------------------------------------


def _write_pointer(claude_projects_dir: Path, project_path: Path, *, pid: int = 81750) -> Path:
    """Write a well-formed, non-live bridge-pointer.json for ``project_path``."""
    pdir = claude_projects_dir / pointers.sanitize_cwd(project_path)
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / "bridge-pointer.json"
    path.write_text(
        json.dumps(
            {
                "sessionId": "session_01LG15p2JVjwBamscENjuBLi",
                "environmentId": "env_01RHE7cHW3DawXjGRp5Ae3va",
                "source": "standalone",
                "pid": pid,  # long-dead PID -> not live
                "procStart": "2590192",
            }
        )
    )
    return path


def test_clear_pointer_absent_returns_false(tmp_path: Path):
    assert pointers.clear_pointer(Path("/no/such/project"), claude_projects_dir=tmp_path) is False


def test_clear_pointer_removes_nonlive_and_backs_up(tmp_path: Path):
    proj = Path("/mnt/nas/projects/alpha")
    path = _write_pointer(tmp_path, proj)
    assert pointers.clear_pointer(proj, claude_projects_dir=tmp_path) is True
    assert not path.exists()
    assert path.with_name(path.name + ".bak").exists()


def test_clear_pointer_no_backup_when_disabled(tmp_path: Path):
    proj = Path("/mnt/nas/projects/alpha")
    path = _write_pointer(tmp_path, proj)
    assert pointers.clear_pointer(proj, claude_projects_dir=tmp_path, backup=False) is True
    assert not path.exists()
    assert not path.with_name(path.name + ".bak").exists()


def test_clear_pointer_refuses_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    proj = Path("/mnt/nas/projects/alpha")
    path = _write_pointer(tmp_path, proj)
    monkeypatch.setattr(pointers, "is_live", lambda ptr: True)
    with pytest.raises(pointers.PointerStillLive):
        pointers.clear_pointer(proj, claude_projects_dir=tmp_path)
    assert path.exists()  # a live anchor is never yanked


def test_clear_pointer_removes_malformed(tmp_path: Path):
    # A corrupt pointer has no derivable liveness; clear it so it can't wedge the next start.
    proj = Path("/mnt/nas/projects/alpha")
    pdir = tmp_path / pointers.sanitize_cwd(proj)
    pdir.mkdir(parents=True)
    path = pdir / "bridge-pointer.json"
    path.write_text("{not json")
    assert pointers.clear_pointer(proj, claude_projects_dir=tmp_path) is True
    assert not path.exists()


def test_clear_pointer_removes_a_hostile_pointer(tmp_path: Path):
    # The one behavioural delta of the malformed -> None widening, and the sibling the test
    # above cannot cover: `{not json` already returned None before the change. A hostile
    # payload used to raise out of load_pointer, clear_pointer propagated it, and the file
    # survived to wedge the next bridge start. Now it degrades to None, the PointerStillLive
    # guard is skipped (no derivable liveness), and the file is backed up and removed.
    proj = Path("/mnt/nas/projects/alpha")
    path = pointers.pointer_path_for(proj, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("[" * 100_000)

    assert pointers.clear_pointer(proj, claude_projects_dir=tmp_path) is True
    assert not path.exists()
    assert path.with_name(path.name + ".bak").exists()  # recoverable


def test_clear_pointer_backup_failure_still_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # The backup is best-effort: a write failure is logged, and the delete still proceeds.
    proj = Path("/mnt/nas/projects/alpha")
    path = _write_pointer(tmp_path, proj)

    def _boom(self: Path, data: bytes) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", _boom)
    assert pointers.clear_pointer(proj, claude_projects_dir=tmp_path) is True
    assert not path.exists()
    assert not path.with_name(path.name + ".bak").exists()
