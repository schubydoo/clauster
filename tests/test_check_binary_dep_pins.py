"""Tests for ``scripts/check_binary_dep_pins.py`` — verify + ``--fix`` of BINARY_DEPS pins.

The script lives under ``scripts/`` (not ``src/``), so it isn't importable as a package; we load
it by path. Every test stubs the network (``_fetch_release``) and, for ``--fix`` runs, points the
module loader at a throwaway file — the real ``src/clauster/deps.py`` is never fetched or written.
"""

from __future__ import annotations

import importlib.util
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
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def script():
    """The freshly-loaded check-script module."""
    return _load_script()


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
