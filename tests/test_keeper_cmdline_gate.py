"""The keeper cmdline gate: a recycled/unrelated PID is never classified or killed (RUNOPS-1)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from clauster import procutil, pty_keeper


def _sidecar(log_dir: Path, name: str, *, keeper_pid: int) -> Path:
    """Write a minimal `<name>-<ms>-<seq>.keeper.json` sidecar pointing at ``keeper_pid``."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{name}-1700000000000-0.keeper.json"
    path.write_text(f'{{"keeper_pid": {keeper_pid}, "state": "ready"}}', encoding="utf-8")
    return path


def test_is_keeper_cmdline_matches_only_the_keeper_module():
    assert procutil.is_keeper_cmdline(["python", "-m", "clauster.pty_keeper", "--sidecar", "x"])
    assert procutil.is_keeper_cmdline(["/usr/bin/python3.11", "-m", "clauster.pty_keeper"])
    assert not procutil.is_keeper_cmdline(["python", "-c", "import time; time.sleep(60)"])
    assert not procutil.is_keeper_cmdline(["claude", "remote-control", "proj"])
    # Carries the module name but isn't a `python -m` keeper → must NOT match:
    assert not procutil.is_keeper_cmdline(
        ["grep", "clauster.pty_keeper", "/var/log/foo"]
    )  # not python
    assert not procutil.is_keeper_cmdline(
        ["python", "-c", "clauster.pty_keeper"]
    )  # data arg, not -m
    assert not procutil.is_keeper_cmdline([])


def test_is_keeper_process_fails_closed_on_a_dead_pid():
    assert procutil.is_keeper_process(2_147_483_646) is False  # NoSuchProcess → fail closed


def test_iter_keepers_rejects_a_live_non_keeper_pid(tmp_path):
    # The pytest process is alive but is NOT a clauster.pty_keeper, so a sidecar pointing
    # at its PID (the recycled-PID case) must classify dead — never a live orphan.
    _sidecar(tmp_path, "ghost", keeper_pid=os.getpid())
    [info] = pty_keeper.iter_keepers(tmp_path)
    assert info.alive is False


def test_stop_keeper_refuses_to_kill_a_non_keeper_pid():
    # A live process that is NOT our keeper (a PID recycled onto a stranger) must never
    # be SIGKILLed: stop_keeper reports it gone but leaves the stranger running.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        ct = procutil.proc_create_time(proc.pid)
        assert ct is not None  # the stranger is alive...
        assert pty_keeper.stop_keeper(proc.pid, expect_create_time=ct) is True
        assert procutil.proc_create_time(proc.pid) is not None  # ...and was NOT killed
    finally:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
