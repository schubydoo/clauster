from __future__ import annotations

from pathlib import Path

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


def test_fixture_pointers_are_not_live(fixtures_dir: Path):
    # The captured pointers reference long-dead PIDs -> never trusted as live.
    for name in ("test1", "test2", "dockerize2"):
        ptr = pointers.load_pointer(fixtures_dir / "pointers" / f"{name}.bridge-pointer.json")
        assert ptr is not None
        assert pointers.is_live(ptr) is False
