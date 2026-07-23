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
    monkeypatch.setattr(script, "_fetch_release", lambda *a: _release("shawl-shawl.zip", "abc123"))
    assert script._published_digest(_dep("abc123")) == ("ok", None)


def test_published_digest_mismatch_returns_correct_value(script, monkeypatch):
    monkeypatch.setattr(script, "_fetch_release", lambda *a: _release("shawl-shawl.zip", "newsha"))
    assert script._published_digest(_dep("oldsha")) == ("mismatch", "newsha")


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


# --- main(): mode wiring + exit codes ---------------------------------------------------------


def _install_deps(script, monkeypatch, deps, path):
    """Point the script's loader at a synthetic ``deps`` tuple + ``path`` (no real import/IO)."""
    monkeypatch.setattr(script, "_load_deps", lambda: (tuple(deps), path))


def test_main_verify_passes_when_all_match(script, monkeypatch, tmp_path, capsys):
    _install_deps(script, monkeypatch, [_dep("abc")], tmp_path / "deps.py")
    monkeypatch.setattr(script, "_fetch_release", lambda *a: _release("shawl-shawl.zip", "abc"))
    assert script.main([]) == 0
    assert "OK" in capsys.readouterr().out


def test_main_verify_fails_and_prints_value_on_mismatch(script, monkeypatch, tmp_path, capsys):
    path = tmp_path / "deps.py"
    path.write_text('sha256="stale"\n', encoding="utf-8")
    _install_deps(script, monkeypatch, [_dep("stale")], path)
    monkeypatch.setattr(script, "_fetch_release", lambda *a: _release("shawl-shawl.zip", "fresh"))
    assert script.main([]) == 1  # verify mode never mutates
    out = capsys.readouterr().out
    assert "FAIL" in out and "fresh" in out
    assert path.read_text(encoding="utf-8") == 'sha256="stale"\n'


def test_main_fix_rewrites_stale_hash_and_passes(script, monkeypatch, tmp_path, capsys):
    path = tmp_path / "deps.py"
    path.write_text('sha256 = "stale"\n', encoding="utf-8")
    _install_deps(script, monkeypatch, [_dep("stale")], path)
    monkeypatch.setattr(script, "_fetch_release", lambda *a: _release("shawl-shawl.zip", "fresh"))
    assert script.main(["--fix"]) == 0
    assert path.read_text(encoding="utf-8") == 'sha256 = "fresh"\n'
    assert "FIX" in capsys.readouterr().out


def test_main_fix_is_noop_when_already_correct(script, monkeypatch, tmp_path):
    path = tmp_path / "deps.py"
    path.write_text('sha256 = "good"\n', encoding="utf-8")
    _install_deps(script, monkeypatch, [_dep("good")], path)
    monkeypatch.setattr(script, "_fetch_release", lambda *a: _release("shawl-shawl.zip", "good"))
    before = path.read_text(encoding="utf-8")
    assert script.main(["--fix"]) == 0
    assert path.read_text(encoding="utf-8") == before  # idempotent: no rewrite when nothing stale


def test_main_fix_still_fails_closed_on_fetch_error(script, monkeypatch, tmp_path):
    path = tmp_path / "deps.py"
    path.write_text('sha256 = "stale"\n', encoding="utf-8")
    _install_deps(script, monkeypatch, [_dep("stale")], path)

    def _boom(*_a):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(script, "_fetch_release", _boom)
    assert script.main(["--fix"]) == 1  # unverifiable bump must not pass
    assert path.read_text(encoding="utf-8") == 'sha256 = "stale"\n'
