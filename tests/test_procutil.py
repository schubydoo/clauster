from __future__ import annotations

import os

import psutil

from clauster import procutil


def test_is_bridge_cmdline_matches_real_bridge():
    assert procutil.is_bridge_cmdline(["claude", "remote-control", "--name", "x"]) is True
    assert procutil.is_bridge_cmdline(["python3", "/p/claude", "remote-control"]) is True


def test_is_bridge_cmdline_rejects_non_bridge():
    assert procutil.is_bridge_cmdline([]) is False
    assert procutil.is_bridge_cmdline(["claude", "agents", "--json"]) is False
    assert procutil.is_bridge_cmdline(["python3", "pytest"]) is False


def test_dead_pid_is_not_live():
    # A PID that almost certainly doesn't exist.
    assert procutil.is_live_bridge(2_000_000_000, None) is False


def test_current_process_is_not_a_bridge():
    # Alive, but cmdline is pytest/python — must fail the cmdline gate.
    assert procutil.is_live_bridge(os.getpid(), None) is False


def test_proc_create_time_of_self_is_float():
    ct = procutil.proc_create_time(os.getpid())
    assert isinstance(ct, float) and ct > 0


def test_jiffies_to_epoch_uses_boot_time():
    epoch = procutil.jiffies_to_epoch(0)
    assert epoch is not None
    assert abs(epoch - psutil.boot_time()) < 1.0


def test_zombie_status_treated_as_dead(monkeypatch):
    class FakeProc:
        def __init__(self, pid):
            pass

        def status(self):
            return psutil.STATUS_ZOMBIE

        def cmdline(self):
            return ["claude", "remote-control"]

        def create_time(self):
            return 123.0

    monkeypatch.setattr(procutil.psutil, "Process", FakeProc)
    assert procutil.is_live_bridge(1234, None) is False


def test_create_time_mismatch_rejected(monkeypatch):
    class FakeProc:
        def __init__(self, pid):
            pass

        def status(self):
            return psutil.STATUS_RUNNING

        def cmdline(self):
            return ["claude", "remote-control"]

        def create_time(self):
            return 1000.0

    monkeypatch.setattr(procutil.psutil, "Process", FakeProc)
    # A float proc_start is our OWN exact create_time, so it must match the same
    # process near-exactly (tight bound). A gap means the PID was recycled.
    assert procutil.is_live_bridge(1234, 5000.0) is False  # far off -> reuse
    assert procutil.is_live_bridge(1234, 1000.5) is False  # 0.5s off -> reuse (was True)
    assert procutil.is_live_bridge(1234, 1000.0) is True  # same measurement
    assert procutil.is_live_bridge(1234, 1000.02) is True  # hair of float jitter is fine


def test_jiffies_pointer_keeps_loose_tolerance(monkeypatch):
    # A pointer's jiffies epoch is derived independently of the live process's
    # create_time, so a genuine same-process match can be a touch off -> keep the
    # looser 2.0s tolerance (the tight exact-float bound would false-negative it).
    monkeypatch.setattr(procutil.psutil, "Process", _fake_proc(ct=1000.0))
    monkeypatch.setattr(procutil, "jiffies_to_epoch", lambda j: 1001.5)
    assert procutil.is_live_bridge(1234, "500") is True  # 1.5s off, within 2.0s
    monkeypatch.setattr(procutil, "jiffies_to_epoch", lambda j: 1003.0)
    assert procutil.is_live_bridge(1234, "500") is False  # 3.0s off, beyond 2.0s


def _fake_proc(status=psutil.STATUS_RUNNING, cmdline=("claude", "remote-control"), ct=1000.0):
    class FakeProc:
        def __init__(self, pid):
            pass

        def status(self):
            return status

        def cmdline(self):
            return list(cmdline)

        def create_time(self):
            return ct

    return FakeProc


def test_is_live_bridge_skips_start_check_when_none(monkeypatch):
    # Bridge cmdline + alive + no comparable start time -> trusted (line 91).
    monkeypatch.setattr(procutil.psutil, "Process", _fake_proc())
    assert procutil.is_live_bridge(1234, None) is True


def test_clk_tck_falls_back_on_error(monkeypatch):
    # raising=False: os.sysconf doesn't exist on Windows, so patch it in regardless.
    monkeypatch.setattr(
        procutil.os, "sysconf", lambda _name: (_ for _ in ()).throw(OSError()), raising=False
    )
    assert procutil._clk_tck() == 100


def test_jiffies_to_epoch_none_when_boot_time_unavailable(monkeypatch):
    monkeypatch.setattr(procutil.psutil, "boot_time", lambda: (_ for _ in ()).throw(OSError()))
    assert procutil.jiffies_to_epoch(500) is None


def test_proc_create_time_zombie_is_none(monkeypatch):
    monkeypatch.setattr(procutil.psutil, "Process", _fake_proc(status=psutil.STATUS_ZOMBIE))
    assert procutil.proc_create_time(1234) is None


def test_proc_create_time_missing_pid_is_none():
    assert procutil.proc_create_time(2_000_000_000) is None


def test_expected_epoch_normalizations():
    assert procutil._expected_epoch(None) is None
    assert procutil._expected_epoch(1234.5) == 1234.5  # already an epoch
    assert procutil._expected_epoch("abc") is None  # non-numeric -> skip
    assert procutil._expected_epoch(True) is None  # bool -> int("True") fails -> None
    jiffies = procutil._expected_epoch("0")  # jiffies string -> epoch
    assert jiffies is not None and abs(jiffies - psutil.boot_time()) < 1.0


def test_reap_if_exited_swallows_non_child():
    # Neither a bogus PID nor our own (not a child) should raise.
    procutil.reap_if_exited(2_000_000_000)
    procutil.reap_if_exited(os.getpid())


def test_force_kill_tree_kills_process():
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert psutil.pid_exists(proc.pid)
        procutil.force_kill_tree(proc.pid)
        proc.wait(timeout=5)
        assert proc.poll() is not None  # actually dead
    finally:
        if proc.poll() is None:
            proc.kill()


def test_force_kill_tree_safe_on_dead_pid():
    procutil.force_kill_tree(2_000_000_000)  # absent PID -> no raise


def test_force_kill_tree_swallows_kill_race(monkeypatch):
    # A target that dies between enumeration and kill (NoSuchProcess on .kill())
    # must be swallowed per-process, not abort the whole tree-kill.
    class Racy:
        def kill(self):
            raise psutil.NoSuchProcess(1234)

    class FakeProc:
        def __init__(self, pid):
            pass

        def children(self, recursive=False):
            return [Racy()]

        def kill(self):
            raise psutil.NoSuchProcess(1234)

    monkeypatch.setattr(procutil.psutil, "Process", FakeProc)
    procutil.force_kill_tree(1234)  # both children and parent race -> no raise
