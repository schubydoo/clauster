"""Tests for ``scripts/check_binary_dep_pins.py`` — verify + ``--fix`` of the pinned sha256s.

The script lives under ``scripts/`` (not ``src/``), so it isn't importable as a package; we load
it by path. Every test stubs the network (``_fetch_release``) and, for ``--fix`` runs, points the
loaders at throwaway files — the real ``deps.py`` / workflow files are never fetched or written.
The two pin sources (BINARY_DEPS and the GitHub Actions workflow pins) share that mocked digest
core, so a ``main()`` test isolates whichever source it does not exercise to an empty set.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import types
import urllib.error
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_binary_dep_pins.py"

# Realistic 64-hex sha256 payloads — the classifier now rejects anything that isn't one, so the
# fakes must look like real digests (a short "abc123" would be classified "error", not "ok").
_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64


def _load_script():
    """Import the check script as a module by file path (it is not an installable package)."""
    spec = importlib.util.spec_from_file_location("check_binary_dep_pins", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so the ``@dataclass`` with ``from __future__ import annotations`` can
    # resolve its string annotations (``Callable``) via ``sys.modules[cls.__module__]`` — running
    # the script as ``__main__`` gets this for free, a by-path load does not.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def script():
    """The freshly-loaded check-script module."""
    return _load_script()


@pytest.fixture(autouse=True)
def _default_no_workflow_pins(script, monkeypatch):
    """Default every test to zero workflow pins so the deps-only tests stay hermetic.

    ``main()`` now checks a second pin source (the GitHub Actions workflow pins). Tests that
    exercise it install their own via ``_install_workflow_pins``; everyone else gets an empty set,
    so the existing BINARY_DEPS tests never read a real workflow file or hit the network for it.
    """
    monkeypatch.setattr(script, "_load_workflow_pins", lambda: ((), Path(".")))


def _dep(sha256: str, *, key: str = "shawl", url: str | None = None) -> types.SimpleNamespace:
    """A minimal stand-in for :class:`clauster.deps.BinaryDep` (only the read fields)."""
    url = url or f"https://github.com/mtkennerly/shawl/releases/download/v1.9.0/shawl-{key}.zip"
    return types.SimpleNamespace(key=key, url=url, sha256=sha256)


def _release(asset_name: str, digest: str | None) -> dict:
    """A GitHub release JSON with one asset, optionally carrying a ``sha256:`` digest."""
    asset: dict = {"name": asset_name}
    if digest is not None:
        asset["digest"] = f"sha256:{digest}"
    return {"assets": [asset]}


# --- _published_digest: the network/lookup classifier -----------------------------------------


def test_published_digest_ok(script, monkeypatch):
    monkeypatch.setattr(script, "_fetch_release", lambda *a: _release("shawl-shawl.zip", _SHA_A))
    assert script._published_digest(_dep(_SHA_A)) == ("ok", None)


def test_published_digest_mismatch_returns_correct_value(script, monkeypatch):
    monkeypatch.setattr(script, "_fetch_release", lambda *a: _release("shawl-shawl.zip", _SHA_B))
    assert script._published_digest(_dep(_SHA_A)) == ("mismatch", _SHA_B)


@pytest.mark.parametrize("bad", ["", "zz", "abc123", "A" * 64, "a" * 63, "a" * 65])
def test_published_digest_error_on_malformed_digest(script, monkeypatch, bad):
    # A sha256:-prefixed but empty / non-hex / uppercase / wrong-length payload must NOT be
    # treated as a new pin to write — it fails closed as an error, never a "mismatch".
    monkeypatch.setattr(script, "_fetch_release", lambda *a: _release("shawl-shawl.zip", bad))
    status, value = script._published_digest(_dep(_SHA_A))
    assert status == "error"
    assert "malformed sha256" in value


def test_published_digest_skip_non_github_url(script):
    status, value = script._published_digest(_dep("x", url="https://example.com/thing.zip"))
    assert status == "skip"
    assert "not a github releases-download URL" in value


def test_published_digest_warn_when_no_published_sha(script, monkeypatch):
    monkeypatch.setattr(script, "_fetch_release", lambda *a: _release("shawl-shawl.zip", None))
    status, value = script._published_digest(_dep("oldsha"))
    assert status == "warn"
    assert "no sha256" in value


def test_published_digest_error_when_asset_absent(script, monkeypatch):
    monkeypatch.setattr(script, "_fetch_release", lambda *a: _release("some-other-asset.zip", "z"))
    status, value = script._published_digest(_dep("oldsha"))
    assert status == "error"
    assert "no asset named" in value


def test_published_digest_error_on_fetch_failure(script, monkeypatch):
    def _boom(*_a):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(script, "_fetch_release", _boom)
    status, value = script._published_digest(_dep("oldsha"))
    assert status == "error"
    assert "could not fetch" in value


# --- _apply_fixes: the in-place rewrite -------------------------------------------------------


def test_apply_fixes_rewrites_each_sha(script, tmp_path):
    f = tmp_path / "deps.py"
    f.write_text('a = "oldsha1"\nb = "oldsha2"\n', encoding="utf-8")
    notes = script._apply_fixes(f, {"oldsha1": "newsha1", "oldsha2": "newsha2"})
    assert notes == []
    assert f.read_text(encoding="utf-8") == 'a = "newsha1"\nb = "newsha2"\n'


def test_apply_fixes_reports_missing_sha_without_writing(script, tmp_path):
    f = tmp_path / "deps.py"
    original = 'a = "present"\n'
    f.write_text(original, encoding="utf-8")
    notes = script._apply_fixes(f, {"absent": "whatever"})
    assert len(notes) == 1
    assert "could not locate" in notes[0]
    assert f.read_text(encoding="utf-8") == original  # nothing applied → not rewritten


def test_apply_fixes_is_all_or_nothing_when_one_sha_is_missing(script, tmp_path):
    # A locatable pin must NOT be rewritten just because a sibling pin can't be found — the file
    # stays byte-identical so a failed run never leaves a partially-reconciled deps.py.
    f = tmp_path / "deps.py"
    original = 'a = "present"\nb = "alsohere"\n'
    f.write_text(original, encoding="utf-8")
    notes = script._apply_fixes(f, {"present": "rewritten", "absent": "whatever"})
    assert len(notes) == 1 and "could not locate" in notes[0]
    assert f.read_text(encoding="utf-8") == original  # located pin left untouched too


# --- main(): mode wiring + exit codes ---------------------------------------------------------


def _install_deps(script, monkeypatch, deps, path):
    """Point the script's loader at a synthetic ``deps`` tuple + ``path`` (no real import/IO)."""
    monkeypatch.setattr(script, "_load_deps", lambda: (tuple(deps), path))


def test_main_verify_passes_when_all_match(script, monkeypatch, tmp_path, capsys):
    _install_deps(script, monkeypatch, [_dep(_SHA_A)], tmp_path / "deps.py")
    monkeypatch.setattr(script, "_fetch_release", lambda *a: _release("shawl-shawl.zip", _SHA_A))
    assert script.main([]) == 0
    assert "OK" in capsys.readouterr().out


def test_main_verify_fails_and_prints_value_on_mismatch(script, monkeypatch, tmp_path, capsys):
    path = tmp_path / "deps.py"
    path.write_text(f'sha256="{_SHA_A}"\n', encoding="utf-8")
    _install_deps(script, monkeypatch, [_dep(_SHA_A)], path)
    monkeypatch.setattr(script, "_fetch_release", lambda *a: _release("shawl-shawl.zip", _SHA_B))
    assert script.main([]) == 1  # verify mode never mutates
    out = capsys.readouterr().out
    assert "FAIL" in out and _SHA_B in out
    assert path.read_text(encoding="utf-8") == f'sha256="{_SHA_A}"\n'


def test_main_fix_rewrites_stale_hash_and_passes(script, monkeypatch, tmp_path, capsys):
    path = tmp_path / "deps.py"
    path.write_text(f'sha256 = "{_SHA_A}"\n', encoding="utf-8")
    _install_deps(script, monkeypatch, [_dep(_SHA_A)], path)
    monkeypatch.setattr(script, "_fetch_release", lambda *a: _release("shawl-shawl.zip", _SHA_B))
    assert script.main(["--fix"]) == 0
    assert path.read_text(encoding="utf-8") == f'sha256 = "{_SHA_B}"\n'
    assert "FIX" in capsys.readouterr().out


def test_main_fix_is_noop_when_already_correct(script, monkeypatch, tmp_path):
    path = tmp_path / "deps.py"
    path.write_text(f'sha256 = "{_SHA_C}"\n', encoding="utf-8")
    _install_deps(script, monkeypatch, [_dep(_SHA_C)], path)
    monkeypatch.setattr(script, "_fetch_release", lambda *a: _release("shawl-shawl.zip", _SHA_C))
    before = path.read_text(encoding="utf-8")
    assert script.main(["--fix"]) == 0
    assert path.read_text(encoding="utf-8") == before  # idempotent: no rewrite when nothing stale


def test_main_fix_still_fails_closed_on_fetch_error(script, monkeypatch, tmp_path):
    path = tmp_path / "deps.py"
    path.write_text(f'sha256 = "{_SHA_A}"\n', encoding="utf-8")
    _install_deps(script, monkeypatch, [_dep(_SHA_A)], path)

    def _boom(*_a):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(script, "_fetch_release", _boom)
    assert script.main(["--fix"]) == 1  # unverifiable bump must not pass
    assert path.read_text(encoding="utf-8") == f'sha256 = "{_SHA_A}"\n'


def test_main_fix_refuses_partial_write_on_other_pin_failure(
    script, monkeypatch, tmp_path, capsys
):
    # One pin is stale (a valid replacement is available) while a *second* pin can't be fetched.
    # The run is already exit-1, so --fix must write NOTHING — no partially-reconciled deps.py.
    path = tmp_path / "deps.py"
    path.write_text(f'stale = "{_SHA_A}"\nother = "{_SHA_C}"\n', encoding="utf-8")
    stale = _dep(_SHA_A, url="https://github.com/mtkennerly/shawl/releases/download/v1/shawl.zip")
    broken = _dep(_SHA_C, url="https://github.com/acme/other/releases/download/v2/other.zip")
    _install_deps(script, monkeypatch, [stale, broken], path)

    def _fetch(owner, repo, tag):
        if repo == "shawl":
            return _release("shawl.zip", _SHA_B)  # stale pin has a fresh digest to apply
        raise urllib.error.URLError("down")  # the other pin is unverifiable

    monkeypatch.setattr(script, "_fetch_release", _fetch)
    before = path.read_text(encoding="utf-8")
    assert script.main(["--fix"]) == 1
    assert path.read_text(encoding="utf-8") == before  # partial reconcile refused
    assert "SKIP" in capsys.readouterr().out


# --- workflow pins: the second source (osv-scanner.yml / actionlint.yml) -----------------------


def _osv_pin(script, filename: str = "osv-scanner.yml"):
    """A synthetic osv-shaped :class:`_WorkflowPin` (constant asset, version-verbatim tag)."""
    return script._WorkflowPin(
        path=filename,
        owner="google",
        repo="osv-scanner",
        version_re=re.compile(r'OSV_VERSION:\s*"([^"]+)"'),
        sha_re=re.compile(r'OSV_SHA256:\s*"([0-9a-f]{64})"'),
        tag=lambda v: v,
        asset=lambda v: "osv-scanner_linux_amd64",
    )


def _write_osv_workflow(path: Path, sha: str, *, version: str = "v2.5.1") -> None:
    """Write a minimal env block carrying the OSV_VERSION + OSV_SHA256 pins the regexes read."""
    path.write_text(
        f'        env:\n          OSV_VERSION: "{version}"\n          OSV_SHA256: "{sha}"\n',
        encoding="utf-8",
    )


def _install_workflow_pins(script, monkeypatch, pins, root):
    """Point the workflow-pin loader at a synthetic table + throwaway ``root`` (no real files)."""
    monkeypatch.setattr(script, "_load_workflow_pins", lambda: (tuple(pins), root))


def test_workflow_pin_derivations_cover_both_shapes(script):
    # The two real pins derive their tag + asset differently — lock both so a future edit to one
    # can't silently regress the other (osv: v-prefixed version, constant asset; actionlint: bare
    # version -> v-prefixed tag, version-embedded tarball).
    by_repo = {pin.repo: pin for pin in script._WORKFLOW_PINS}
    osv = by_repo["osv-scanner"]
    assert osv.tag("v2.5.1") == "v2.5.1"
    assert osv.asset("v2.5.1") == "osv-scanner_linux_amd64"
    actionlint = by_repo["actionlint"]
    assert actionlint.tag("1.7.12") == "v1.7.12"
    assert actionlint.asset("1.7.12") == "actionlint_1.7.12_linux_amd64.tar.gz"


def test_real_workflow_pins_are_parseable_in_the_committed_files(script):
    # Guard the regexes against a workflow reformat: every real pin must still locate its VERSION +
    # 64-hex SHA256 in its committed file, and derive a well-formed tag/asset. No network.
    pins, root = script._WORKFLOW_PINS, script._repo_root()
    assert {pin.repo for pin in pins} == {"osv-scanner", "actionlint"}
    for pin in pins:
        text = (root / pin.path).read_text(encoding="utf-8")
        vmatch = pin.version_re.search(text)
        smatch = pin.sha_re.search(text)
        assert vmatch and smatch, f"{pin.path}: VERSION/SHA256 pin not found by its regex"
        assert re.fullmatch(r"[0-9a-f]{64}", smatch.group(1))  # a real committed asset digest
        assert pin.tag(vmatch.group(1)).startswith("v")
        assert pin.asset(vmatch.group(1))  # non-empty derived asset name


def test_resolve_workflow_pin_ok(script, monkeypatch, tmp_path):
    _write_osv_workflow(tmp_path / "osv-scanner.yml", _SHA_A)
    monkeypatch.setattr(
        script, "_fetch_release", lambda *a: _release("osv-scanner_linux_amd64", _SHA_A)
    )
    assert script._resolve_workflow_pin(_osv_pin(script), tmp_path) == ("ok", None, _SHA_A)


def test_resolve_workflow_pin_mismatch_returns_correct_value(script, monkeypatch, tmp_path):
    _write_osv_workflow(tmp_path / "osv-scanner.yml", _SHA_A)
    monkeypatch.setattr(
        script, "_fetch_release", lambda *a: _release("osv-scanner_linux_amd64", _SHA_B)
    )
    assert script._resolve_workflow_pin(_osv_pin(script), tmp_path) == ("mismatch", _SHA_B, _SHA_A)


def test_resolve_workflow_pin_error_on_unparseable_file(script, tmp_path):
    (tmp_path / "osv-scanner.yml").write_text("no pins here\n", encoding="utf-8")
    status, value, current = script._resolve_workflow_pin(_osv_pin(script), tmp_path)
    assert status == "error"
    assert "could not find the VERSION/SHA256 pin" in value
    assert current is None


def test_resolve_workflow_pin_error_on_missing_file(script, tmp_path):
    status, value, current = script._resolve_workflow_pin(_osv_pin(script), tmp_path)
    assert status == "error"
    assert "could not read" in value
    assert current is None


def test_resolve_workflow_pin_error_on_fetch_failure(script, monkeypatch, tmp_path):
    _write_osv_workflow(tmp_path / "osv-scanner.yml", _SHA_A)

    def _boom(*_a):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(script, "_fetch_release", _boom)
    status, value, current = script._resolve_workflow_pin(_osv_pin(script), tmp_path)
    assert status == "error"
    assert "could not fetch" in value
    assert current == _SHA_A  # sha parsed from the file even though the fetch failed


def test_main_verify_passes_when_workflow_pin_matches(script, monkeypatch, tmp_path, capsys):
    _install_deps(script, monkeypatch, [], tmp_path / "deps.py")  # BINARY_DEPS source empty
    _write_osv_workflow(tmp_path / "osv-scanner.yml", _SHA_A)
    _install_workflow_pins(script, monkeypatch, [_osv_pin(script)], tmp_path)
    monkeypatch.setattr(
        script, "_fetch_release", lambda *a: _release("osv-scanner_linux_amd64", _SHA_A)
    )
    assert script.main([]) == 0
    assert "OK" in capsys.readouterr().out


def test_main_verify_fails_on_workflow_mismatch_without_rewrite(
    script, monkeypatch, tmp_path, capsys
):
    _install_deps(script, monkeypatch, [], tmp_path / "deps.py")
    wf = tmp_path / "osv-scanner.yml"
    _write_osv_workflow(wf, _SHA_A)
    _install_workflow_pins(script, monkeypatch, [_osv_pin(script)], tmp_path)
    monkeypatch.setattr(
        script, "_fetch_release", lambda *a: _release("osv-scanner_linux_amd64", _SHA_B)
    )
    before = wf.read_text(encoding="utf-8")
    assert script.main([]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and _SHA_B in out
    assert wf.read_text(encoding="utf-8") == before  # verify never mutates the workflow file


def test_main_fix_rewrites_stale_workflow_sha(script, monkeypatch, tmp_path, capsys):
    _install_deps(script, monkeypatch, [], tmp_path / "deps.py")
    wf = tmp_path / "osv-scanner.yml"
    _write_osv_workflow(wf, _SHA_A)
    _install_workflow_pins(script, monkeypatch, [_osv_pin(script)], tmp_path)
    monkeypatch.setattr(
        script, "_fetch_release", lambda *a: _release("osv-scanner_linux_amd64", _SHA_B)
    )
    assert script.main(["--fix"]) == 0
    rewritten = wf.read_text(encoding="utf-8")
    assert _SHA_B in rewritten and _SHA_A not in rewritten
    assert "FIX" in capsys.readouterr().out


def test_main_fix_warns_and_passes_when_workflow_asset_has_no_digest(
    script, monkeypatch, tmp_path, capsys
):
    # An asset GitHub publishes no sha256 for (e.g. a pre-digest release) warns and passes without
    # a rewrite — nothing to retry into existence, so a --fix run must leave the file untouched.
    _install_deps(script, monkeypatch, [], tmp_path / "deps.py")
    wf = tmp_path / "osv-scanner.yml"
    _write_osv_workflow(wf, _SHA_A)
    _install_workflow_pins(script, monkeypatch, [_osv_pin(script)], tmp_path)
    monkeypatch.setattr(
        script, "_fetch_release", lambda *a: _release("osv-scanner_linux_amd64", None)
    )
    before = wf.read_text(encoding="utf-8")
    assert script.main(["--fix"]) == 0
    assert "WARN" in capsys.readouterr().out
    assert wf.read_text(encoding="utf-8") == before


def test_main_fix_fails_closed_when_stale_sha_absent_from_file(
    script, monkeypatch, tmp_path, capsys
):
    # The cross-file pre-flight net: a fix candidate whose old sha isn't present in its target file
    # at write time is reported and the run fails closed, writing nothing (cross-file atomicity).
    path = tmp_path / "deps.py"
    path.write_text('sha256 = "not-the-pinned-value"\n', encoding="utf-8")
    _install_deps(script, monkeypatch, [_dep(_SHA_A)], path)
    monkeypatch.setattr(script, "_fetch_release", lambda *a: _release("shawl-shawl.zip", _SHA_B))
    before = path.read_text(encoding="utf-8")
    assert script.main(["--fix"]) == 1
    assert "could not locate" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == before  # nothing written


def test_main_fix_preflight_is_atomic_across_files(script, monkeypatch, tmp_path, capsys):
    # Two fixable mismatches in DIFFERENT files, zero verify failures — but one file's old sha is
    # absent at write time. The cross-file pre-flight must refuse the WHOLE run, so the other,
    # perfectly locatable file is left untouched too. This is the pre-flight's unique guarantee
    # over _apply_fixes's own per-file guard: cross-file atomicity, not just per-file.
    deps_path = tmp_path / "deps.py"
    deps_path.write_text('sha256 = "not-the-pinned-value"\n', encoding="utf-8")  # _SHA_A absent
    _install_deps(script, monkeypatch, [_dep(_SHA_A)], deps_path)
    wf = tmp_path / "osv-scanner.yml"
    _write_osv_workflow(wf, _SHA_C)  # the workflow pin IS present and fixable
    _install_workflow_pins(script, monkeypatch, [_osv_pin(script)], tmp_path)

    def _fetch(owner, repo, tag):
        if repo == "shawl":
            return _release("shawl-shawl.zip", _SHA_B)  # deps mismatch, candidate not in the file
        return _release(
            "osv-scanner_linux_amd64", _SHA_B
        )  # workflow mismatch, candidate locatable

    monkeypatch.setattr(script, "_fetch_release", _fetch)
    wf_before = wf.read_text(encoding="utf-8")
    assert script.main(["--fix"]) == 1
    assert "could not locate" in capsys.readouterr().out
    assert wf.read_text(encoding="utf-8") == wf_before  # locatable file left untouched too


def test_main_fix_workflow_fails_closed_on_fetch_error(script, monkeypatch, tmp_path):
    _install_deps(script, monkeypatch, [], tmp_path / "deps.py")
    wf = tmp_path / "osv-scanner.yml"
    _write_osv_workflow(wf, _SHA_A)
    _install_workflow_pins(script, monkeypatch, [_osv_pin(script)], tmp_path)

    def _boom(*_a):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(script, "_fetch_release", _boom)
    before = wf.read_text(encoding="utf-8")
    assert script.main(["--fix"]) == 1  # unverifiable bump must not pass
    assert wf.read_text(encoding="utf-8") == before


def test_main_fix_refuses_partial_write_across_both_sources(script, monkeypatch, tmp_path, capsys):
    # A deps pin is stale-but-fixable while a workflow pin can't be fetched. The run is exit-1, so
    # --fix must write NOTHING to EITHER file — cross-source all-or-nothing, not just per-file.
    deps_path = tmp_path / "deps.py"
    deps_path.write_text(f'sha256 = "{_SHA_A}"\n', encoding="utf-8")
    _install_deps(script, monkeypatch, [_dep(_SHA_A)], deps_path)
    wf = tmp_path / "osv-scanner.yml"
    _write_osv_workflow(wf, _SHA_C)
    _install_workflow_pins(script, monkeypatch, [_osv_pin(script)], tmp_path)

    def _fetch(owner, repo, tag):
        if repo == "shawl":
            return _release("shawl-shawl.zip", _SHA_B)  # deps pin has a fresh digest to apply
        raise urllib.error.URLError("down")  # the workflow pin is unverifiable

    monkeypatch.setattr(script, "_fetch_release", _fetch)
    deps_before, wf_before = deps_path.read_text(), wf.read_text()
    assert script.main(["--fix"]) == 1
    assert deps_path.read_text() == deps_before  # deps pin NOT written despite being fixable
    assert wf.read_text() == wf_before
    assert "SKIP" in capsys.readouterr().out
