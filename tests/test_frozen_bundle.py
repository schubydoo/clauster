"""The frozen binary's unpack-directory guard (#1663).

An age-based temp cleanup deletes the one-file build's unpacked files under a
long-running service. These tests cover the guard that re-stamps those files, the
missing-file count it keeps, and the two surfaces that report it: ``/healthz`` (503) and
the dashboard doctor panel.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster import frozen_bundle
from clauster.app import create_app
from clauster.config import load_config
from clauster.frozen_bundle import BundleGuard

_OLD = (1_000_000.0, 1_000_000.0)  # 1970: older than any cleanup window


def _tree(root: Path) -> list[Path]:
    """Build a small unpack-like tree under ``root`` and age every path in it."""
    (root / "clauster" / "templates").mkdir(parents=True)
    files = [root / "libpython.so", root / "clauster" / "templates" / "dashboard.html"]
    for f in files:
        f.write_text("x", encoding="utf-8")
    paths = [root, root / "clauster", root / "clauster" / "templates", *files]
    for p in reversed(paths):  # children first: creating a child re-stamps its parent
        os.utime(p, _OLD)
    return paths


def test_unpack_dir_is_none_from_source():
    assert frozen_bundle.unpack_dir() is None


def test_unpack_dir_is_meipass_when_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert frozen_bundle.unpack_dir() == tmp_path


def test_unpack_dir_is_none_when_frozen_without_meipass(monkeypatch):
    # A frozen build that is not one-file (nothing unpacked) has nothing to guard.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert frozen_bundle.unpack_dir() is None


def test_guard_without_root_is_inert():
    guard = BundleGuard(None)
    assert guard.refresh() == 0
    assert guard.missing == 0


def test_refresh_restamps_every_file_and_directory(tmp_path):
    paths = _tree(tmp_path / "_MEIabc")
    guard = BundleGuard(tmp_path / "_MEIabc")
    before = time.time() - 5
    assert guard.refresh() == 0
    for p in paths:
        assert p.stat().st_mtime >= before, p
        assert p.stat().st_atime >= before, p


@pytest.mark.skipif(
    os.utime not in os.supports_follow_symlinks, reason="platform cannot stamp a symlink"
)
def test_refresh_stamps_a_symlink_itself(tmp_path):
    # A cleanup ages a symlink by its own timestamps, not its target's.
    root = tmp_path / "_MEIabc"
    _tree(root)
    link = root / "libpython.so.1"
    link.symlink_to("libpython.so")
    os.utime(link, _OLD, follow_symlinks=False)
    BundleGuard(root).refresh()
    assert link.lstat().st_mtime >= time.time() - 5


def test_refresh_counts_and_logs_missing_files(tmp_path, caplog):
    root = tmp_path / "_MEIabc"
    paths = _tree(root)
    guard = BundleGuard(root)
    paths[-1].unlink()  # what the cleanup does to dashboard.html
    with caplog.at_level(logging.ERROR, logger="clauster.frozen_bundle"):
        assert guard.refresh() == 1
    assert guard.missing == 1
    assert "1 of 5 unpacked files are missing" in caplog.text
    # A later pass with the file back (a restart, in practice) clears the count.
    paths[-1].write_text("x", encoding="utf-8")
    assert guard.refresh() == 0
    assert guard.missing == 0


def test_refresh_warns_but_does_not_count_an_unstampable_file(tmp_path, monkeypatch, caplog):
    root = tmp_path / "_MEIabc"
    _tree(root)
    guard = BundleGuard(root)

    def _deny(path, **kwargs):
        raise PermissionError(13, "denied", path)

    monkeypatch.setattr(frozen_bundle.os, "utime", _deny)
    with caplog.at_level(logging.WARNING, logger="clauster.frozen_bundle"):
        assert guard.refresh() == 0
    assert "cannot refresh unpacked file" in caplog.text


async def test_run_refreshes_repeatedly_until_cancelled(tmp_path):
    guard = BundleGuard(tmp_path)
    calls = 0

    def _count() -> int:
        nonlocal calls
        calls += 1
        return 0

    guard.refresh = _count  # type: ignore[method-assign]
    task = asyncio.create_task(guard.run(interval=0.01))
    for _ in range(200):
        if calls >= 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls >= 2


def _app(write_config, tmp_path):
    return create_app(load_config(write_config(f"state_dir: {tmp_path / '.s'}\n")))


def test_lifespan_restamps_the_unpack_dir_when_frozen(write_config, tmp_path, monkeypatch):
    root = tmp_path / "_MEIabc"
    paths = _tree(root)
    monkeypatch.setattr(frozen_bundle, "unpack_dir", lambda: root)
    with TestClient(_app(write_config, tmp_path)) as client:
        deadline = time.time() + 10
        while paths[-1].stat().st_mtime < 2_000_000 and time.time() < deadline:
            time.sleep(0.02)
        assert client.get("/healthz").status_code == 200
    assert paths[-1].stat().st_mtime > 2_000_000


def test_healthz_is_ok_from_source(write_config, tmp_path):
    app = _app(write_config, tmp_path)
    assert app.state.bundle_guard.root is None
    r = TestClient(app).get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_healthz_fails_when_unpacked_files_are_missing(write_config, tmp_path):
    app = _app(write_config, tmp_path)
    app.state.bundle_guard.missing = 3
    r = TestClient(app).get("/healthz")
    assert r.status_code == 503
    assert r.json() == {"detail": "unpacked program files are missing; restart Clauster"}


def test_doctor_panel_has_no_bundle_check_from_source(write_config, tmp_path):
    body = TestClient(_app(write_config, tmp_path)).get("/api/doctor").json()
    assert "bundle" not in {c["name"] for c in body["checks"]}


def test_doctor_panel_reports_the_unpack_dir_when_frozen(write_config, tmp_path):
    root = tmp_path / "_MEIabc"
    paths = _tree(root)
    app = _app(write_config, tmp_path)
    guard = app.state.bundle_guard = BundleGuard(root)
    client = TestClient(app)

    def _bundle() -> tuple[dict, bool]:
        body = client.get("/api/doctor").json()
        return next(c for c in body["checks"] if c["name"] == "bundle"), body["ok"]

    check, _ok = _bundle()
    assert check["status"] == "ok"
    assert str(root) in check["detail"]

    paths[-1].unlink()
    guard.refresh()
    check, ok = _bundle()
    assert check["status"] == "fail"
    assert check["detail"].startswith("1 unpacked program file(s) missing")
    assert ok is False
