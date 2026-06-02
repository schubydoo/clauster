"""Tests for the PTY keeper (true-resume sidecar). POSIX only — there is no `pty` on Windows."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="pty keeper is POSIX-only")

# A fake bridge: prints a connect URL (the keeper scrapes it off the PTY), then
# idles until signalled — the flag-form bridge's shape, minus the real network.
_FAKE_BRIDGE = """
import signal, sys, time
sys.stdout.write("Continue at https://claude.ai/code/session_01KEEPERTESTAAAAAAAAAA\\r\\n")
sys.stdout.flush()
stop = {"v": False}
signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("v", True))
signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("v", True))
for _ in range(2400):
    if stop["v"]:
        break
    time.sleep(0.05)
"""


def _read(sidecar: Path) -> dict:
    try:
        return json.loads(sidecar.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def test_keeper_publishes_pid_and_connect_url(tmp_path: Path) -> None:
    """The keeper runs the bridge under a PTY and writes a discoverable sidecar."""
    bridge = tmp_path / "fake_bridge.py"
    bridge.write_text(_FAKE_BRIDGE)
    sidecar = tmp_path / "k.json"
    keeper = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "clauster.pty_keeper",
            "--sidecar",
            str(sidecar),
            "--",
            sys.executable,
            str(bridge),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # mimic Clauster's detached launch
    )
    try:
        info: dict = {}
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            info = _read(sidecar)
            if info.get("connect_url"):
                break
            time.sleep(0.1)

        assert info.get("state") == "ready"
        assert info.get("connect_url") == "https://claude.ai/code/session_01KEEPERTESTAAAAAAAAAA"
        assert info.get("session_id") == "session_01KEEPERTESTAAAAAAAAAA"
        assert isinstance(info.get("bridge_pid"), int)
        assert info["bridge_pid"] != keeper.pid  # the bridge is the keeper's child
        assert info.get("keeper_pid") == keeper.pid

        # Stopping the bridge (what Clauster does) makes the keeper self-exit.
        os.kill(info["bridge_pid"], signal.SIGINT)
        keeper.wait(timeout=10)
        assert _read(sidecar).get("state") == "exited"
    finally:
        if keeper.poll() is None:
            try:
                os.killpg(os.getpgid(keeper.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            keeper.wait(timeout=5)


def test_keeper_records_error_when_bridge_cannot_spawn(tmp_path: Path) -> None:
    """A bridge that fails to exec is recorded as state=error, not a hang."""
    sidecar = tmp_path / "k.json"
    rc = subprocess.run(
        [
            sys.executable,
            "-m",
            "clauster.pty_keeper",
            "--sidecar",
            str(sidecar),
            "--",
            str(tmp_path / "does-not-exist-binary"),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=15,
    )
    assert rc.returncode != 0
    info = _read(sidecar)
    assert info.get("state") == "error"
    assert info.get("error")


def test_keeper_main_requires_bridge_argv(tmp_path: Path) -> None:
    """Invoking the keeper with no bridge argv after `--` is a usage error."""
    rc = subprocess.run(
        [sys.executable, "-m", "clauster.pty_keeper", "--sidecar", str(tmp_path / "k.json")],
        capture_output=True,
        timeout=15,
    )
    assert rc.returncode != 0
    assert b"bridge argv" in rc.stderr


# ----- in-process (so the keeper module is covered, unlike the subprocess tests) -----


@pytest.fixture
def _restore_sighup():
    """run_keeper sets SIGHUP=SIG_IGN in-process; restore the runner's handler after."""
    old = signal.getsignal(signal.SIGHUP)
    yield
    signal.signal(signal.SIGHUP, old)


def test_run_keeper_inprocess_scrapes_url(tmp_path: Path, _restore_sighup) -> None:
    """run_keeper scrapes the connect URL off the PTY and records the bridge's exit."""
    from clauster import pty_keeper

    sidecar = tmp_path / "k.json"
    bridge = [
        sys.executable,
        "-c",
        "import sys,time;"
        "sys.stdout.write('at https://claude.ai/code/session_01INPROCAAAAAAAAA\\r\\n');"
        "sys.stdout.flush(); time.sleep(0.6)",
    ]
    rc = pty_keeper.run_keeper(bridge, sidecar, cwd=str(tmp_path))
    assert rc == 0
    info = _read(sidecar)
    assert info["session_id"] == "session_01INPROCAAAAAAAAA"
    assert info["connect_url"].endswith("session_01INPROCAAAAAAAAA")
    assert info["state"] == "exited"
    assert isinstance(info["bridge_pid"], int)


def test_run_keeper_inprocess_spawn_failure(tmp_path: Path, _restore_sighup) -> None:
    """A bridge that can't be exec'd is recorded as an error, not raised."""
    from clauster import pty_keeper

    sidecar = tmp_path / "k.json"
    rc = pty_keeper.run_keeper([str(tmp_path / "nope")], sidecar, cwd=str(tmp_path))
    assert rc != 0
    assert _read(sidecar)["state"] == "error"


def test_main_strips_leading_separator(tmp_path: Path, _restore_sighup) -> None:
    """main() drops the `--` separator argparse keeps in REMAINDER before exec."""
    from clauster import pty_keeper

    sidecar = tmp_path / "k.json"
    rc = pty_keeper.main(["--sidecar", str(sidecar), "--", sys.executable, "-c", "pass"])
    assert rc == 0
    assert _read(sidecar)["state"] == "exited"


def test_main_requires_bridge_argv_inprocess(tmp_path: Path) -> None:
    """No bridge argv after the options is a usage error, not a hang."""
    from clauster import pty_keeper

    with pytest.raises(SystemExit):
        pty_keeper.main(["--sidecar", str(tmp_path / "k.json")])


def test_proc_start_returns_none_on_error(monkeypatch) -> None:
    """A failure resolving the bridge's start time degrades to None, never raises."""
    from clauster import pty_keeper

    def _boom(_pid):
        raise RuntimeError("psutil exploded")

    monkeypatch.setattr("clauster.procutil.proc_create_time", _boom)
    assert pty_keeper._proc_start(1234) is None


def test_write_sidecar_swallows_oserror(tmp_path: Path) -> None:
    """A sidecar write into a missing directory must never take the bridge down."""
    from clauster import pty_keeper

    pty_keeper._write_sidecar(tmp_path / "no" / "such" / "k.json", {"a": 1})  # no raise


def test_run_keeper_openpty_failure(tmp_path: Path, monkeypatch, _restore_sighup) -> None:
    """A PTY allocation failure is recorded as an error and returned, not raised."""
    import pty as _pty

    from clauster import pty_keeper

    def _boom():
        raise OSError("no ptys available")

    monkeypatch.setattr(_pty, "openpty", _boom)
    sidecar = tmp_path / "k.json"
    rc = pty_keeper.run_keeper([sys.executable, "-c", "pass"], sidecar, cwd=str(tmp_path))
    assert rc == 70
    assert _read(sidecar)["state"] == "error"


def test_run_keeper_url_timeout(tmp_path: Path, monkeypatch, _restore_sighup) -> None:
    """If the bridge never prints a URL, the keeper stops buffering and still exits cleanly."""
    from clauster import pty_keeper

    monkeypatch.setattr(pty_keeper, "_URL_TIMEOUT", 0.2)
    sidecar = tmp_path / "k.json"
    rc = pty_keeper.run_keeper(
        [sys.executable, "-c", "import time; time.sleep(0.6)"], sidecar, cwd=str(tmp_path)
    )
    assert rc == 0
    info = _read(sidecar)
    assert info["connect_url"] is None
    assert info["state"] == "exited"
