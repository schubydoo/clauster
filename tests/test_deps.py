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


def test_install_hint_points_at_docs_when_frozen(monkeypatch):
    # The frozen binary ignores site-packages, so `pip install` is a dead end — and the managed
    # `clauster deps install` command is a later slice, so the hint must NOT name it yet (that
    # would be a dead-end command). Point at the docs until slice 2 ships the command.
    monkeypatch.setattr(deps, "is_frozen", lambda: True)
    hint = deps.install_hint(deps.by_key("pyte"))
    assert "standalone binary" in hint  # prose, not a command
    assert "clauster deps install" not in hint  # not a not-yet-real command
    assert "pip install" not in hint  # pip is a dead end on the frozen binary


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


def test_remove_distribution_swallows_unlink_errors(tmp_path):
    # A RECORD entry whose unlink() fails (here it resolves to a directory) is best-effort —
    # swallowed, never propagated. (importlib pre-filters already-absent files, so this is the
    # path that actually exercises the guard.)
    import types

    a_dir = tmp_path / "notafile"
    a_dir.mkdir()
    fake = types.SimpleNamespace(files=["notafile"], locate_file=lambda _rel: str(a_dir))
    deps._remove_distribution(fake, tmp_path)  # OSError from unlinking a dir must not escape
    assert a_dir.exists()  # unremovable entry left in place, no crash


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


def test_remove_distribution_skips_unresolvable_entry(monkeypatch, tmp_path):
    # If a RECORD entry's path can't be resolved (OSError), skip it rather than crash.
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
    deps._remove_distribution(fake, target)  # boundary resolves; the entry is skipped, no raise


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
