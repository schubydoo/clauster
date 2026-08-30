"""Optional-extras detection (#904): registry, side-effect-free probe, frozen-aware hints.

Also the slice-2 managed side-install orchestration: the deps-dir path, the frozen-only
``sys.path`` prepend, and the pip-driven install/list/uninstall (with the pip call and the
confirmation prompt stubbed — no network, no real pip).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from clauster import deps


def _fake_dist(target: Path, name: str, version: str, modules: list[str]) -> None:
    """Materialise a minimal wheel layout (package files + ``.dist-info`` METADATA/RECORD).

    Enough for :func:`importlib.metadata.distributions` to discover ``name``/``version`` and for
    ``dist.files`` (the RECORD) to drive :func:`deps.uninstall_extra`'s per-file removal.
    """
    target.mkdir(parents=True, exist_ok=True)
    record_lines: list[str] = []
    for rel in modules:
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# stub\n", encoding="utf-8")
        record_lines.append(f"{rel},,")
    info = target / f"{name}-{version}.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8"
    )
    record_lines += [
        f"{name}-{version}.dist-info/METADATA,,",
        f"{name}-{version}.dist-info/RECORD,,",
    ]
    (info / "RECORD").write_text("\n".join(record_lines) + "\n", encoding="utf-8")


def test_registry_covers_the_three_extras():
    keys = {e.key for e in deps.EXTRAS}
    assert keys == {"pyte", "pywinpty", "apprise"}
    # pyte + pywinpty both belong to the `pty` extra; apprise to `notify`.
    assert {e.extra_name for e in deps.EXTRAS} == {"pty", "notify"}
    # pywinpty is the only platform-scoped entry (win32); the rest are cross-platform.
    assert deps.by_key("pywinpty").platform_marker == "win32"
    assert deps.by_key("pyte").platform_marker is None
    assert deps.by_key("apprise").platform_marker is None


def test_by_key_unknown_raises():
    with pytest.raises(KeyError):
        deps.by_key("nope")


def test_applies_honours_platform_marker(monkeypatch):
    win = deps.by_key("pywinpty")
    monkeypatch.setattr(deps.sys, "platform", "linux")
    assert deps.applies(win) is False
    assert deps.applies(deps.by_key("pyte")) is True  # None marker applies everywhere
    monkeypatch.setattr(deps.sys, "platform", "win32")
    assert deps.applies(win) is True


def test_probe_present_when_find_spec_returns_a_spec(monkeypatch):
    monkeypatch.setattr(deps.importlib.util, "find_spec", lambda name: object())
    assert deps.probe(deps.by_key("pyte")) is True


def test_probe_absent_when_find_spec_returns_none(monkeypatch):
    monkeypatch.setattr(deps.importlib.util, "find_spec", lambda name: None)
    assert deps.probe(deps.by_key("pyte")) is False


@pytest.mark.parametrize("exc", [ImportError("x"), ModuleNotFoundError("x"), ValueError("x")])
def test_probe_swallows_lookup_errors_as_absent(monkeypatch, exc):
    # A submodule whose parent isn't a package raises ModuleNotFoundError; a half-initialised
    # module raises ValueError — both must read as "absent", never propagate out of a probe.
    def _raise(name):
        raise exc

    monkeypatch.setattr(deps.importlib.util, "find_spec", _raise)
    assert deps.probe(deps.by_key("apprise")) is False


def test_probe_never_imports_the_module(monkeypatch):
    # The whole point of find_spec over import: no side effects (LGPL pyte is never executed).
    called = {}
    monkeypatch.setattr(
        deps.importlib.util, "find_spec", lambda name: called.setdefault("n", name)
    )
    deps.probe(deps.by_key("pywinpty"))
    assert called["n"] == "winpty"  # probes the IMPORT name, not the dist name


def test_is_frozen_reflects_sys_frozen(monkeypatch):
    monkeypatch.delattr(deps.sys, "frozen", raising=False)
    assert deps.is_frozen() is False
    monkeypatch.setattr(deps.sys, "frozen", True, raising=False)
    assert deps.is_frozen() is True


def test_install_hint_pip_form_when_not_frozen(monkeypatch):
    monkeypatch.setattr(deps, "is_frozen", lambda: False)
    assert deps.install_hint(deps.by_key("pyte")) == "pip install 'clauster[pty]'"
    assert deps.install_hint(deps.by_key("apprise")) == "pip install 'clauster[notify]'"


def test_install_hint_names_deps_command_when_frozen(monkeypatch):
    # The frozen binary bundles pip (2b) and offers the managed `clauster deps install <extra>`
    # command — a real, runnable command, so the hint names it (not the dead-end `pip install`).
    monkeypatch.setattr(deps, "is_frozen", lambda: True)
    assert deps.install_hint(deps.by_key("pyte")) == "clauster deps install pty"
    assert deps.install_hint(deps.by_key("apprise")) == "clauster deps install notify"
    assert "pip install" not in deps.install_hint(deps.by_key("pyte"))  # pip is a dead end frozen


# ----- registry helpers ----------------------------------------------------


def test_extra_names_are_distinct_in_registry_order():
    # pyte + pywinpty collapse to one `pty`; apprise is `notify`. Order follows EXTRAS.
    assert deps.extra_names() == ("pty", "notify")


def test_extras_for_groups_by_extra_name_all_platforms():
    # extras_for does NOT platform-filter (that's the caller's job) — pywinpty shows on Linux too.
    pty = {e.key for e in deps.extras_for("pty")}
    assert pty == {"pyte", "pywinpty"}
    assert {e.key for e in deps.extras_for("notify")} == {"apprise"}
    assert deps.extras_for("nope") == ()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("PyYAML", "pyyaml"), ("pywinpty", "pywinpty"), ("a_b.c", "a-b-c"), ("--x__y--", "x-y")],
)
def test_canonical_name_pep503(raw, expected):
    assert deps.canonical_name(raw) == expected


def test_managed_deps_dir_is_state_dir_slash_deps(tmp_path):
    assert deps.managed_deps_dir(tmp_path) == tmp_path / "deps"
    assert deps.managed_deps_dir(str(tmp_path)) == tmp_path / "deps"


# ----- add_deps_dir_to_sys_path (frozen-only startup prepend) ---------------


def test_add_deps_dir_noop_when_not_frozen(monkeypatch, tmp_path):
    (tmp_path / "deps").mkdir()
    monkeypatch.setattr(deps, "is_frozen", lambda: False)
    monkeypatch.setattr(sys, "path", sys.path[:])
    deps.add_deps_dir_to_sys_path(tmp_path)
    assert str(tmp_path / "deps") not in sys.path


def test_add_deps_dir_appends_when_frozen_and_dir_exists(monkeypatch, tmp_path):
    (tmp_path / "deps").mkdir()
    monkeypatch.setattr(deps, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "path", sys.path[:])
    deps.add_deps_dir_to_sys_path(tmp_path)
    # APPENDED (never prepended) so a bundled/installed copy always wins over the side-install.
    assert sys.path[-1] == str(tmp_path / "deps")


def test_add_deps_dir_noop_when_dir_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(deps, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "path", sys.path[:])
    deps.add_deps_dir_to_sys_path(tmp_path)  # no <state_dir>/deps created
    assert str(tmp_path / "deps") not in sys.path


def test_add_deps_dir_is_idempotent(monkeypatch, tmp_path):
    (tmp_path / "deps").mkdir()
    monkeypatch.setattr(deps, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "path", sys.path[:])
    deps.add_deps_dir_to_sys_path(tmp_path)
    deps.add_deps_dir_to_sys_path(tmp_path)
    assert sys.path.count(str(tmp_path / "deps")) == 1


def test_add_deps_dir_makes_a_side_installed_module_importable(monkeypatch, tmp_path):
    # The end-to-end contract: a module dropped in <state_dir>/deps imports after the prepend.
    depsdir = tmp_path / "deps"
    depsdir.mkdir()
    (depsdir / "clauster_frozen_probe.py").write_text("VALUE = 41\n", encoding="utf-8")
    monkeypatch.setattr(deps, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "path", sys.path[:])
    monkeypatch.delitem(sys.modules, "clauster_frozen_probe", raising=False)
    deps.add_deps_dir_to_sys_path(tmp_path)
    import importlib

    mod = importlib.import_module("clauster_frozen_probe")
    try:
        assert mod.VALUE == 41
    finally:
        sys.modules.pop("clauster_frozen_probe", None)


# ----- installed_versions --------------------------------------------------


def test_installed_versions_empty_when_no_deps_dir(tmp_path):
    assert deps.installed_versions(tmp_path) == {}


def test_installed_versions_reads_managed_dir(tmp_path):
    _fake_dist(tmp_path / "deps", "apprise", "1.9.0", ["apprise/__init__.py"])
    assert deps.installed_versions(tmp_path) == {"apprise": "1.9.0"}


def test_installed_versions_skips_dist_without_name(tmp_path):
    # A dist-info whose METADATA has no Name is skipped, never crashes the listing.
    depsdir = tmp_path / "deps"
    info = depsdir / "weird-1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nVersion: 1.0\n", encoding="utf-8")
    assert deps.installed_versions(tmp_path) == {}


def test_add_deps_dir_swallows_path_errors(monkeypatch, tmp_path):
    # A frozen build with an unresolvable state_dir must not raise from startup augmentation.
    monkeypatch.setattr(deps, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "path", sys.path[:])

    def _boom(_sd):
        raise OSError("bad path")

    monkeypatch.setattr(deps, "managed_deps_dir", _boom)
    deps.add_deps_dir_to_sys_path(tmp_path)  # returns cleanly, nothing appended
    assert all("deps" not in p for p in sys.path[len(sys.path) - 1 :])


def test_remove_distribution_reports_failure_on_undeletable_file(tmp_path):
    # A RECORD entry whose unlink() fails (here a directory; models a locked Windows .pyd) must be
    # reported as a FAILED removal (False), not swallowed as success — the file survives on disk.
    import types

    stuck = tmp_path / "stuck"
    stuck.mkdir()
    fake = types.SimpleNamespace(files=["stuck"], locate_file=lambda _rel: str(stuck))
    assert deps._remove_distribution(fake, tmp_path) is False  # OSError caught, but not "removed"
    assert stuck.exists()  # left in place, no crash


def test_remove_distribution_treats_already_gone_file_as_removed(tmp_path):
    # A RECORD entry that vanished before unlink (race) raises FileNotFoundError → not left behind,
    # so removal still counts as success (nothing of the extra remains).
    import types

    target = tmp_path / "deps"
    target.mkdir()
    fake = types.SimpleNamespace(files=["gone"], locate_file=lambda _rel: str(target / "gone"))
    assert deps._remove_distribution(fake, target) is True


def test_uninstall_extra_reports_incomplete_on_undeletable_file(tmp_path, capsys):
    # A RECORD-listed file that can't be unlinked (locked .pyd, permission denied) must make
    # uninstall report failure + exit 1, not falsely claim "removed" (#933 review round 2).
    depsdir = tmp_path / "deps"
    (depsdir / "apprise").mkdir(parents=True)
    (depsdir / "apprise" / "stuck").mkdir()  # a dir where a file is listed → unlink fails
    info = depsdir / "apprise-1.9.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: apprise\nVersion: 1.9.0\n", encoding="utf-8"
    )
    (info / "RECORD").write_text(
        "apprise/stuck,,\napprise-1.9.0.dist-info/METADATA,,\napprise-1.9.0.dist-info/RECORD,,\n",
        encoding="utf-8",
    )
    rc = deps.uninstall_extra("notify", tmp_path)
    assert rc == 1
    err = capsys.readouterr().err
    assert "could not fully remove" in err and "removed" not in err
    assert (
        depsdir / "apprise" / "stuck"
    ).exists()  # the undeletable file survives, honestly reported


def test_remove_distribution_refuses_to_escape_managed_dir(tmp_path):
    # A tampered RECORD with a `../`-escaping entry must NOT unlink a file outside the managed
    # dir — the containment guard mirrors _prune_empty_dirs' boundary (defense-in-depth).
    import types

    target = tmp_path / "deps"
    target.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("precious", encoding="utf-8")
    fake = types.SimpleNamespace(
        files=["../outside.txt"], locate_file=lambda _rel: str(target / ".." / "outside.txt")
    )
    deps._remove_distribution(fake, target)
    assert outside.exists()  # escaping entry left untouched


def test_remove_distribution_reports_failure_on_unresolvable_entry(monkeypatch, tmp_path):
    # If a RECORD entry's path can't even be resolved (OSError, e.g. a symlink loop), fail closed:
    # the entry is unprocessable so removal can't be confirmed → return False, never crash.
    import types

    target = tmp_path / "deps"
    target.mkdir()
    real_resolve = deps.Path.resolve

    def _selective(self, *a, **k):
        if "boom" in str(self):
            raise OSError("cannot resolve")
        return real_resolve(self, *a, **k)

    monkeypatch.setattr(deps.Path, "resolve", _selective)
    fake = types.SimpleNamespace(files=["boom"], locate_file=lambda _rel: str(target / "boom"))
    assert deps._remove_distribution(fake, target) is False  # unprocessable → not a clean removal


def test_remove_distribution_one_survivor_fails_the_whole_dist(tmp_path):
    # Mixed manifest: one file removes cleanly, one can't (a dir → unlink fails). A single survivor
    # must flip the whole distribution to failure, and the removable file is still deleted.
    import types

    target = tmp_path / "deps"
    (target / "pkg").mkdir(parents=True)
    gone = target / "pkg" / "mod.py"
    gone.write_text("x", encoding="utf-8")
    stuck = target / "pkg" / "locked"
    stuck.mkdir()  # unlink fails → survivor
    fake = types.SimpleNamespace(
        files=["pkg/mod.py", "pkg/locked"],
        locate_file=lambda rel: str(target / rel),
    )
    assert deps._remove_distribution(fake, target) is False  # one survivor fails the dist
    assert not gone.exists()  # the removable file was still deleted
    assert stuck.exists()  # the survivor remains, honestly reported


def test_prune_empty_dirs_swallows_resolve_error(monkeypatch, tmp_path):
    def _boom(self, *a, **k):
        raise OSError("cannot resolve")

    monkeypatch.setattr(deps.Path, "resolve", _boom)
    deps._prune_empty_dirs(tmp_path, stop=tmp_path)  # no raise despite resolve() failing


# ----- install_extra -------------------------------------------------------


def test_install_extra_unknown_returns_2(tmp_path, capsys):
    assert deps.install_extra("nope", tmp_path, assume_yes=True) == 2
    assert "unknown extra" in capsys.readouterr().err


def test_install_extra_declines_on_closed_stdin(tmp_path, capsys):
    # A non-interactive run without --yes (piped/closed stdin) fails closed with a clean
    # message, never an uncaught EOFError traceback.
    def _eof(_prompt):
        raise EOFError

    calls: list[list[str]] = []
    rc = deps.install_extra(
        "notify", tmp_path, pip_main=lambda a: calls.append(a) or 0, confirm=_eof
    )
    assert rc == 1
    assert calls == []
    assert "aborted" in capsys.readouterr().err


def test_install_extra_declined_does_not_call_pip(tmp_path):
    calls: list[list[str]] = []
    rc = deps.install_extra(
        "notify", tmp_path, pip_main=lambda a: calls.append(a) or 0, confirm=lambda _p: "n"
    )
    assert rc == 1
    assert calls == []  # a declined confirmation must not fetch anything


def test_install_extra_assume_yes_invokes_pip_with_target(tmp_path):
    calls: list[list[str]] = []
    rc = deps.install_extra(
        "notify", tmp_path, assume_yes=True, pip_main=lambda a: calls.append(a) or 0
    )
    assert rc == 0
    (argv,) = calls
    assert argv[:3] == ["install", "--target", str(tmp_path / "deps")]
    assert "apprise" in argv and "--upgrade" in argv


def test_install_extra_yes_prompt_accepts(tmp_path):
    rc = deps.install_extra("notify", tmp_path, pip_main=lambda _a: 0, confirm=lambda _p: "  YES ")
    assert rc == 0


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_install_extra_pty_filters_by_platform(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(deps.sys, "platform", platform)
    calls: list[list[str]] = []
    deps.install_extra("pty", tmp_path, assume_yes=True, pip_main=lambda a: calls.append(a) or 0)
    (argv,) = calls
    assert "pyte" in argv  # cross-platform, always
    assert ("pywinpty" in argv) is (platform == "win32")  # win-only extra gated by applies()


def test_install_extra_pip_failure_returns_1(tmp_path, capsys):
    rc = deps.install_extra("notify", tmp_path, assume_yes=True, pip_main=lambda _a: 7)
    assert rc == 1
    assert "pip install failed (exit 7)" in capsys.readouterr().err


def test_install_extra_pip_unavailable_returns_1(tmp_path, capsys):
    def _boom(_argv):
        raise deps.DepsPipUnavailableError("no pip here")

    assert deps.install_extra("notify", tmp_path, assume_yes=True, pip_main=_boom) == 1
    assert "no pip here" in capsys.readouterr().err


def test_install_extra_mkdir_failure_returns_1(tmp_path, capsys):
    # A regular file where the managed dir should go makes mkdir raise — surfaced, not swallowed.
    (tmp_path / "deps").write_text("not a dir", encoding="utf-8")
    rc = deps.install_extra("notify", tmp_path, assume_yes=True, pip_main=lambda _a: 0)
    assert rc == 1
    assert "could not create" in capsys.readouterr().err


# ----- uninstall_extra -----------------------------------------------------


def test_uninstall_extra_unknown_returns_2(tmp_path, capsys):
    assert deps.uninstall_extra("nope", tmp_path) == 2
    assert "unknown extra" in capsys.readouterr().err


def test_uninstall_extra_absent_is_a_noop(tmp_path, capsys):
    assert deps.uninstall_extra("notify", tmp_path) == 0
    assert "not installed" in capsys.readouterr().err


def test_uninstall_extra_removes_record_files_and_prunes(tmp_path, capsys):
    depsdir = tmp_path / "deps"
    _fake_dist(depsdir, "apprise", "1.9.0", ["apprise/__init__.py", "apprise/plugins/base.py"])
    assert (depsdir / "apprise" / "__init__.py").exists()
    rc = deps.uninstall_extra("notify", tmp_path)
    assert rc == 0
    assert "removed apprise" in capsys.readouterr().err
    # Every RECORD file gone, and the now-empty package + dist-info dirs pruned.
    assert not (depsdir / "apprise").exists()
    assert not list(depsdir.glob("apprise-*.dist-info"))


def test_uninstall_extra_tolerates_a_missing_record_file(tmp_path, capsys):
    # A RECORD listing a file that's already gone is best-effort: removal still succeeds.
    depsdir = tmp_path / "deps"
    _fake_dist(depsdir, "apprise", "1.9.0", ["apprise/__init__.py", "apprise/extra.py"])
    (depsdir / "apprise" / "extra.py").unlink()  # vanish before uninstall
    assert deps.uninstall_extra("notify", tmp_path) == 0
    assert "removed apprise" in capsys.readouterr().err
    assert not (depsdir / "apprise").exists()


def test_uninstall_extra_reports_incomplete_without_record(tmp_path, capsys):
    # A dist with no RECORD manifest can't be removed — uninstall must NOT falsely claim success
    # (the files would remain and reload on restart). Fail loud + non-zero, leave files in place.
    depsdir = tmp_path / "deps"
    (depsdir / "apprise").mkdir(parents=True)
    (depsdir / "apprise" / "__init__.py").write_text("x", encoding="utf-8")
    info = depsdir / "apprise-1.9.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: apprise\nVersion: 1.9.0\n", encoding="utf-8"
    )  # deliberately NO RECORD
    rc = deps.uninstall_extra("notify", tmp_path)
    assert rc == 1
    err = capsys.readouterr().err
    assert "no RECORD" in err and "removed" not in err
    assert (depsdir / "apprise" / "__init__.py").exists()  # left on disk, not falsely "removed"


def test_uninstall_extra_leaves_other_extras(tmp_path):
    depsdir = tmp_path / "deps"
    _fake_dist(depsdir, "apprise", "1.9.0", ["apprise/__init__.py"])
    _fake_dist(depsdir, "pyte", "0.8.2", ["pyte/__init__.py"])
    deps.uninstall_extra("notify", tmp_path)  # removes apprise only
    assert not (depsdir / "apprise").exists()
    assert (depsdir / "pyte" / "__init__.py").exists()  # pty extra untouched


# ----- _default_pip_main ---------------------------------------------------


def test_default_pip_main_raises_when_pip_absent(monkeypatch):
    import importlib

    def _no_pip(name):
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", _no_pip)
    with pytest.raises(deps.DepsPipUnavailableError):
        deps._default_pip_main(["install", "x"])


def test_default_pip_main_delegates_to_pip_cli(monkeypatch):
    import importlib
    import types

    seen: list[list[str]] = []
    fake = types.SimpleNamespace(main=lambda argv: seen.append(argv) or 3)
    monkeypatch.setattr(importlib, "import_module", lambda name: fake)
    assert deps._default_pip_main(["install", "x"]) == 3
    assert seen == [["install", "x"]]


# ----- managed binary dependencies (shawl, #904 slice 2b) ------------------


def _fake_shawl_zip(content: bytes = b"MZ-fake-shawl") -> tuple[bytes, str]:
    """Build an in-memory zip containing ``shawl.exe`` + return (bytes, sha256)."""
    import hashlib
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("shawl.exe", content)
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def _fake_claustrum_targz(content: bytes = b"\x7fELF-fake-claustrum") -> tuple[bytes, str]:
    """Build an in-memory ``.tar.gz`` with a root ``claustrum`` member; return (bytes, sha256)."""
    import hashlib
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        info = tarfile.TarInfo("claustrum")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def _install_fake_claustrum(monkeypatch, content: bytes = b"\x7fELF-fake-claustrum"):
    """Register a fake linux/x86_64 claustrum BinaryDep for a fake tar.gz; return (bytes, dep)."""
    data, sha = _fake_claustrum_targz(content)
    dep = deps.BinaryDep(
        key="claustrum",
        label="claustrum (test)",
        platform_marker="linux",
        arch_marker="x86_64",
        version="v1.7.1",
        url="https://example.invalid/claustrum_1.7.1_Linux_x86_64.tar.gz",
        sha256=sha,
        member="claustrum",
        dest="claustrum",
    )
    monkeypatch.setattr(deps, "BINARY_DEPS", (dep,))
    monkeypatch.setattr(deps.sys, "platform", "linux")
    monkeypatch.setattr(deps.platform, "machine", lambda: "x86_64")
    return data, dep


def test_extract_member_targz_and_zip_and_missing():
    tg, _ = _fake_claustrum_targz(b"BODY")
    assert deps._extract_member(tg, "x/claustrum_Linux_x86_64.tar.gz", "claustrum") == b"BODY"
    zp, _ = _fake_shawl_zip(b"ZBODY")
    assert deps._extract_member(zp, "x/shawl.zip", "shawl.exe") == b"ZBODY"
    # A missing member raises KeyError from both backends (install_binary_dep catches it).
    with pytest.raises(KeyError):
        deps._extract_member(tg, "x/a.tar.gz", "nope")
    with pytest.raises(KeyError):
        deps._extract_member(zp, "x/a.zip", "nope")
    # A tar whose member name is a DIRECTORY (extractfile -> None) is refused, not silently empty.
    import io as _io
    import tarfile as _tf

    buf = _io.BytesIO()
    with _tf.open(fileobj=buf, mode="w:gz") as archive:
        info = _tf.TarInfo("claustrum")
        info.type = _tf.DIRTYPE
        archive.addfile(info)
    with pytest.raises(ValueError, match="not a regular file"):
        deps._extract_member(buf.getvalue(), "x/a.tar.gz", "claustrum")
    # A SYMLINK member named `claustrum` must be refused, not silently followed to another
    # in-archive entry's bytes (defense-in-depth on top of the checksum pin).
    buf2 = _io.BytesIO()
    with _tf.open(fileobj=buf2, mode="w:gz") as archive:
        archive.addfile(_tf.TarInfo("real"), _io.BytesIO(b"secret"))
        link = _tf.TarInfo("claustrum")
        link.type = _tf.SYMTYPE
        link.linkname = "real"
        archive.addfile(link)
    with pytest.raises(ValueError, match="not a regular file"):
        deps._extract_member(buf2.getvalue(), "x/a.tar.gz", "claustrum")


def test_install_binary_dep_targz_downloads_verifies_places(tmp_path, monkeypatch):
    data, _dep = _install_fake_claustrum(monkeypatch)
    rc = deps.install_binary_dep("claustrum", tmp_path, assume_yes=True, fetch=lambda _u: data)
    assert rc == 0
    exe = deps.managed_bin_dir(tmp_path) / "claustrum"
    assert exe.read_bytes() == b"\x7fELF-fake-claustrum"


def test_install_binary_dep_reports_write_error_even_when_cleanup_fails(
    tmp_path, monkeypatch, capsys
):
    # The atomic-swap error arm cleans up its temp best-effort; a cleanup that ALSO fails
    # (e.g. the same dying disk) must stay silent and the ORIGINAL write error is reported.
    data, _dep = _install_fake_claustrum(monkeypatch)
    cleanup_attempts: list[Path] = []

    def _replace_boom(self, target):
        raise OSError("disk full")

    def _unlink_boom(self, missing_ok=False):
        cleanup_attempts.append(self)
        raise OSError("cleanup also failed")

    monkeypatch.setattr(Path, "replace", _replace_boom)
    monkeypatch.setattr(Path, "unlink", _unlink_boom)
    rc = deps.install_binary_dep("claustrum", tmp_path, assume_yes=True, fetch=lambda _u: data)
    assert rc == 1
    assert cleanup_attempts, "the error arm must attempt best-effort temp cleanup"
    err = capsys.readouterr().err
    assert "could not write" in err and "disk full" in err
    assert "cleanup also failed" not in err  # the original error wins


def test_install_binary_dep_targz_checksum_mismatch_refuses(tmp_path, monkeypatch):
    _install_fake_claustrum(monkeypatch)
    rc = deps.install_binary_dep(
        "claustrum", tmp_path, assume_yes=True, fetch=lambda _u: b"tampered"
    )
    assert rc == 1
    assert not (deps.managed_bin_dir(tmp_path) / "claustrum").exists()


def _install_fake_shawl(monkeypatch, content: bytes = b"MZ-fake-shawl"):
    """Register a fake win32 shawl BinaryDep matching a fake zip; return (zip bytes, dep)."""
    data, sha = _fake_shawl_zip(content)
    dep = deps.BinaryDep(
        key="shawl",
        label="Shawl (test)",
        platform_marker="win32",
        version="vT",
        url="https://example.invalid/shawl.zip",
        sha256=sha,
        member="shawl.exe",
        dest="shawl.exe",
    )
    monkeypatch.setattr(deps, "BINARY_DEPS", (dep,))
    monkeypatch.setattr(deps.sys, "platform", "win32")  # so applies() passes
    return data, dep


def test_binary_dep_registry_and_lookup():
    assert deps.binary_dep_names() == ("shawl", "claustrum")  # de-duped keys, order preserved
    assert deps.binary_dep_for("shawl").platform_marker == "win32"
    with pytest.raises(KeyError):
        deps.binary_dep_for("nope")


def test_claustrum_variants_registered_for_every_platform_arch():
    # One row per (OS, arch); each carries an arch_marker + a matching pinned url, and the Windows
    # rows extract claustrum.exe while POSIX rows extract claustrum.
    cl = [d for d in deps.BINARY_DEPS if d.key == "claustrum"]
    assert len(cl) == 6
    pairs = {(d.platform_marker, d.arch_marker) for d in cl}
    assert pairs == {
        ("linux", "x86_64"),
        ("linux", "arm64"),
        ("darwin", "x86_64"),
        ("darwin", "arm64"),
        ("win32", "x86_64"),
        ("win32", "arm64"),
    }
    for d in cl:
        assert d.arch_marker in d.url and d.version in d.url
        assert (
            d.member
            == d.dest
            == ("claustrum.exe" if d.platform_marker == "win32" else "claustrum")
        )


def test_host_arch_normalisation(monkeypatch):
    for machine, want in [
        ("x86_64", "x86_64"),
        ("AMD64", "x86_64"),
        ("aarch64", "arm64"),
        ("arm64", "arm64"),
        ("ARM64", "arm64"),
    ]:
        monkeypatch.setattr(deps.platform, "machine", lambda m=machine: m)
        assert deps.host_arch() == want


def test_resolve_binary_dep_picks_variant_and_none_off_matrix(monkeypatch):
    monkeypatch.setattr(deps.sys, "platform", "darwin")
    monkeypatch.setattr(deps.platform, "machine", lambda: "arm64")
    got = deps.resolve_binary_dep("claustrum")
    assert got is not None and got.platform_marker == "darwin" and got.arch_marker == "arm64"
    # An unsupported arch resolves to nothing (never a wrong-arch archive).
    monkeypatch.setattr(deps.platform, "machine", lambda: "riscv64")
    assert deps.resolve_binary_dep("claustrum") is None


def test_managed_bin_dir_is_deps_slash_bin(tmp_path):
    assert deps.managed_bin_dir(tmp_path) == tmp_path / "deps" / "bin"


def test_installed_binary_path_none_then_present(tmp_path, monkeypatch):
    monkeypatch.setattr(deps.sys, "platform", "win32")  # shawl only resolves on win32
    assert deps.installed_binary_path("shawl", tmp_path) is None
    exe = deps.managed_bin_dir(tmp_path) / "shawl.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"x")
    assert deps.installed_binary_path("shawl", tmp_path) == exe


def test_installed_binary_path_none_off_platform(tmp_path, monkeypatch):
    # An off-platform/arch binary resolves to no variant, so its managed path is never "installed"
    # even if a stray file exists — the daemon fallback then correctly reports it absent.
    monkeypatch.setattr(deps.sys, "platform", "linux")
    assert deps.installed_binary_path("shawl", tmp_path) is None


def test_resolve_effective_binary_prefers_configured_on_path(tmp_path, monkeypatch):
    # #1013: an explicit/PATH hit on the configured value wins outright — even an absolute
    # path the operator set (the documented minimal-PATH workaround) resolves, so a presence
    # check can't call it "unavailable".
    monkeypatch.setattr(
        deps.shutil, "which", lambda name: "/opt/go/bin/claustrum" if "claustrum" in name else None
    )
    assert (
        deps.resolve_effective_binary("claustrum", "/opt/go/bin/claustrum", "claustrum", tmp_path)
        == "/opt/go/bin/claustrum"
    )


def test_resolve_effective_binary_default_falls_back_to_managed(tmp_path, monkeypatch):
    # With the DEFAULT binary and nothing on PATH, the managed <state_dir>/deps/bin install is
    # the fallback (mirrors `deps install claustrum`).
    monkeypatch.setattr(deps.sys, "platform", "win32")  # claustrum.exe resolves on win32
    monkeypatch.setattr(deps.shutil, "which", lambda name: None)  # PATH miss
    assert deps.resolve_effective_binary("claustrum", "claustrum", "claustrum", tmp_path) is None
    exe = deps.managed_bin_dir(tmp_path) / "claustrum.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"x")
    assert deps.resolve_effective_binary("claustrum", "claustrum", "claustrum", tmp_path) == str(
        exe
    )


def test_resolve_effective_binary_explicit_missing_does_not_fall_back(tmp_path, monkeypatch):
    # An operator who configured a NON-default binary that doesn't resolve must see it fail —
    # never silently substitute the managed install of a possibly-different version.
    monkeypatch.setattr(deps.sys, "platform", "win32")
    monkeypatch.setattr(deps.shutil, "which", lambda name: None)
    exe = deps.managed_bin_dir(tmp_path) / "claustrum.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"x")  # managed IS installed, but the configured path is explicit + missing
    assert (
        deps.resolve_effective_binary("claustrum", "/nope/claustrum", "claustrum", tmp_path)
        is None
    )


def test_install_binary_dep_unknown_returns_2(tmp_path, capsys):
    assert deps.install_binary_dep("nope", tmp_path, assume_yes=True) == 2
    assert "unknown binary" in capsys.readouterr().err


def test_install_binary_dep_off_platform_returns_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(deps.sys, "platform", "linux")  # shawl is win32-only
    assert deps.install_binary_dep("shawl", tmp_path, assume_yes=True) == 2
    assert "no shawl build for this platform/arch" in capsys.readouterr().err


def test_install_binary_dep_downloads_verifies_places(tmp_path, monkeypatch):
    data, _dep = _install_fake_shawl(monkeypatch)
    rc = deps.install_binary_dep("shawl", tmp_path, assume_yes=True, fetch=lambda _url: data)
    assert rc == 0
    exe = deps.managed_bin_dir(tmp_path) / "shawl.exe"
    assert exe.read_bytes() == b"MZ-fake-shawl"


def test_install_binary_dep_declined_does_not_fetch(tmp_path, monkeypatch):
    _install_fake_shawl(monkeypatch)
    calls: list[str] = []
    rc = deps.install_binary_dep(
        "shawl", tmp_path, fetch=lambda u: calls.append(u) or b"", confirm=lambda _p: "n"
    )
    assert rc == 1 and calls == []


def test_install_binary_dep_eof_declines(tmp_path, monkeypatch, capsys):
    _install_fake_shawl(monkeypatch)

    def _eof(_p):
        raise EOFError

    rc = deps.install_binary_dep("shawl", tmp_path, fetch=lambda _u: b"x", confirm=_eof)
    assert rc == 1 and "aborted" in capsys.readouterr().err


def test_install_binary_dep_checksum_mismatch_refuses(tmp_path, monkeypatch, capsys):
    _install_fake_shawl(monkeypatch)  # dep.sha256 matches the fake zip
    rc = deps.install_binary_dep("shawl", tmp_path, assume_yes=True, fetch=lambda _u: b"tampered")
    assert rc == 1
    assert "checksum mismatch" in capsys.readouterr().err
    assert not (deps.managed_bin_dir(tmp_path) / "shawl.exe").exists()  # nothing written


def test_install_binary_dep_download_failure_returns_1(tmp_path, monkeypatch, capsys):
    _install_fake_shawl(monkeypatch)

    def _boom(_url):
        raise OSError("network down")

    assert deps.install_binary_dep("shawl", tmp_path, assume_yes=True, fetch=_boom) == 1
    assert "download failed" in capsys.readouterr().err


def test_install_binary_dep_bad_zip_returns_1(tmp_path, monkeypatch, capsys):
    # A payload matching the pinned sha but not a valid zip → surfaced, not a crash.
    import hashlib

    junk = b"not a zip"
    dep = deps.BinaryDep(
        key="shawl",
        label="Shawl",
        platform_marker="win32",
        version="vT",
        url="https://example.invalid/x.zip",
        sha256=hashlib.sha256(junk).hexdigest(),
        member="shawl.exe",
        dest="shawl.exe",
    )
    monkeypatch.setattr(deps, "BINARY_DEPS", (dep,))
    monkeypatch.setattr(deps.sys, "platform", "win32")
    rc = deps.install_binary_dep("shawl", tmp_path, assume_yes=True, fetch=lambda _u: junk)
    assert rc == 1
    assert "could not extract" in capsys.readouterr().err


def test_install_binary_dep_write_failure_returns_1(tmp_path, monkeypatch, capsys):
    data, _dep = _install_fake_shawl(monkeypatch)
    # A regular file where the bin dir should go makes the mkdir/write fail.
    (tmp_path / "deps").mkdir()
    (tmp_path / "deps" / "bin").write_text("not a dir", encoding="utf-8")
    rc = deps.install_binary_dep("shawl", tmp_path, assume_yes=True, fetch=lambda _u: data)
    assert rc == 1
    assert "could not write" in capsys.readouterr().err


def test_default_fetch_refuses_non_https():
    with pytest.raises(ValueError, match="non-https"):
        deps._default_fetch("http://insecure.example/x.zip")


def test_uninstall_binary_dep_unknown_returns_2(tmp_path, capsys):
    assert deps.uninstall_binary_dep("nope", tmp_path) == 2
    assert "unknown binary" in capsys.readouterr().err


def test_uninstall_binary_dep_absent_is_a_noop(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(deps.sys, "platform", "win32")  # shawl only resolves on win32
    assert deps.uninstall_binary_dep("shawl", tmp_path) == 0
    assert "not installed" in capsys.readouterr().err


def test_uninstall_binary_dep_off_platform_is_a_noop(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(deps.sys, "platform", "linux")  # no shawl build here
    assert deps.uninstall_binary_dep("shawl", tmp_path) == 0
    assert "no build for this platform/arch" in capsys.readouterr().err


def test_uninstall_binary_dep_removes_and_prunes(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(deps.sys, "platform", "win32")
    bindir = deps.managed_bin_dir(tmp_path)
    bindir.mkdir(parents=True)
    (bindir / "shawl.exe").write_bytes(b"x")
    rc = deps.uninstall_binary_dep("shawl", tmp_path)
    assert rc == 0
    assert "removed shawl" in capsys.readouterr().err
    assert not (bindir / "shawl.exe").exists()
    assert not bindir.exists()  # emptied bin dir pruned


def test_default_fetch_reads_https(monkeypatch):
    import urllib.request

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, amt=None):  # the cap arg is passed now (_MAX_FETCH_BYTES)
            assert amt == deps._MAX_FETCH_BYTES
            return b"PAYLOAD"

    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=60: _Resp())
    assert deps._default_fetch("https://example.invalid/x.zip") == b"PAYLOAD"


def test_install_binary_dep_confirm_accept_proceeds(tmp_path, monkeypatch):
    data, _dep = _install_fake_shawl(monkeypatch)
    rc = deps.install_binary_dep("shawl", tmp_path, fetch=lambda _u: data, confirm=lambda _p: "y")
    assert rc == 0
    assert (deps.managed_bin_dir(tmp_path) / "shawl.exe").exists()


def test_uninstall_binary_dep_unlink_error_returns_1(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(deps.sys, "platform", "win32")
    bindir = deps.managed_bin_dir(tmp_path)
    bindir.mkdir(parents=True)
    (bindir / "shawl.exe").mkdir()  # a directory where the exe is → unlink raises OSError
    rc = deps.uninstall_binary_dep("shawl", tmp_path)
    assert rc == 1 and "could not remove" in capsys.readouterr().err


def test_install_binary_dep_atomic_replaces_existing(tmp_path, monkeypatch):
    # Installing over an existing shawl.exe replaces it atomically and leaves no .tmp behind
    # (a partial write must never truncate the working binary).
    data, _dep = _install_fake_shawl(monkeypatch)
    bindir = deps.managed_bin_dir(tmp_path)
    bindir.mkdir(parents=True)
    (bindir / "shawl.exe").write_bytes(b"OLD-BINARY")
    rc = deps.install_binary_dep("shawl", tmp_path, assume_yes=True, fetch=lambda _u: data)
    assert rc == 0
    assert (bindir / "shawl.exe").read_bytes() == b"MZ-fake-shawl"
    assert not (bindir / "shawl.exe.tmp").exists()
