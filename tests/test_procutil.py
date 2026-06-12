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


# -- hosted cmdline + generic liveness + match-gated kill (CL-8) -------------


def test_is_hosted_cmdline_matches_stream_json_agent():
    assert procutil.is_hosted_cmdline(["claude", "--output-format", "stream-json"]) is True


def test_is_hosted_cmdline_rejects_bridge_and_empty():
    assert procutil.is_hosted_cmdline(["claude", "remote-control"]) is False
    assert procutil.is_hosted_cmdline([]) is False


def test_is_live_process_no_cmdline_gate(monkeypatch):
    # Without a cmdline predicate, alive + matching create_time is enough.
    monkeypatch.setattr(procutil.psutil, "Process", _fake_proc(cmdline=("anything",), ct=1000.0))
    assert procutil.is_live_process(1234, 1000.0) is True
    assert procutil.is_live_process(1234, 5000.0) is False  # create_time mismatch → reuse


def test_is_live_process_hosted_cmdline_gate(monkeypatch):
    monkeypatch.setattr(
        procutil.psutil, "Process", _fake_proc(cmdline=("claude", "stream-json"), ct=1000.0)
    )
    assert (
        procutil.is_live_process(1234, 1000.0, require_cmdline=procutil.is_hosted_cmdline) is True
    )
    # A bridge cmdline fails the hosted gate even though the process is alive + matches.
    monkeypatch.setattr(
        procutil.psutil, "Process", _fake_proc(cmdline=("claude", "remote-control"), ct=1000.0)
    )
    assert (
        procutil.is_live_process(1234, 1000.0, require_cmdline=procutil.is_hosted_cmdline) is False
    )


def test_is_live_bridge_still_gated_on_bridge_cmdline(monkeypatch):
    # Regression: the wrapper keeps the bridge cmdline gate after the extraction.
    monkeypatch.setattr(
        procutil.psutil, "Process", _fake_proc(cmdline=("claude", "stream-json"), ct=1000.0)
    )
    assert procutil.is_live_bridge(1234, 1000.0) is False  # hosted cmdline ≠ bridge


def test_kill_if_match_kills_only_on_match(monkeypatch):
    killed: list[int] = []
    monkeypatch.setattr(procutil, "force_kill_tree", lambda pid: killed.append(pid))
    monkeypatch.setattr(procutil, "is_live_process", lambda *a, **k: True)
    assert procutil.kill_if_match(1234, 1000.0) is True
    assert killed == [1234]
    killed.clear()
    monkeypatch.setattr(procutil, "is_live_process", lambda *a, **k: False)
    assert procutil.kill_if_match(1234, 1000.0) is False
    assert killed == []  # never killed when the match fails


def test_kill_if_match_fails_closed_without_comparable_start(monkeypatch):
    # Even if liveness would pass, a missing/uncomparable proc_start must NOT kill:
    # without a create-time match this destructive path could hit a reused PID.
    killed: list[int] = []
    monkeypatch.setattr(procutil, "force_kill_tree", lambda pid: killed.append(pid))
    monkeypatch.setattr(procutil, "is_live_process", lambda *a, **k: True)
    assert procutil.kill_if_match(1234, None) is False
    assert procutil.kill_if_match(1234, "garbage") is False
    assert killed == []


def test_is_killable_hosted_requires_comparable_start(monkeypatch):
    # The shared orphan-classification/kill predicate: alive + comparable create-time.
    monkeypatch.setattr(procutil, "is_live_process", lambda *a, **k: True)
    assert procutil.is_killable_hosted(1234, 1000.0) is True  # comparable + alive
    assert procutil.is_killable_hosted(1234, None) is False  # no create-time evidence
    assert procutil.is_killable_hosted(1234, "garbage") is False  # uncomparable
    monkeypatch.setattr(procutil, "is_live_process", lambda *a, **k: False)
    assert procutil.is_killable_hosted(1234, 1000.0) is False  # comparable but dead
