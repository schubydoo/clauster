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


def test_is_keeper_cmdline_matches_the_frozen_subcommand_form():
    # A frozen (PyInstaller) build runs `<exe> __pty-keeper__ …`; sys.executable is the
    # clauster binary there, so `-m` is impossible. The gate must recognize this form, or
    # orphan classification / hard-kill would never see a frozen keeper.
    sub = procutil.KEEPER_SUBCOMMAND
    assert procutil.is_keeper_cmdline(["/opt/clauster/clauster", sub, "--sidecar", "x"])
    # The subcommand must sit in the exact argv[1] slot (right after the exe): a process
    # merely carrying the token as a data argument is NOT a keeper (no spoofing the kill).
    assert not procutil.is_keeper_cmdline(["grep", sub, "/var/log/foo"])
    assert not procutil.is_keeper_cmdline(["clauster", "run", sub])
    assert not procutil.is_keeper_cmdline([sub])  # token alone, no exe in argv[0]


def test_is_keeper_process_fails_closed_on_a_dead_pid():
    assert procutil.is_keeper_process(2_147_483_646) is False  # NoSuchProcess → fail closed


def test_iter_keepers_rejects_a_live_non_keeper_pid(tmp_path):
    # The pytest process is alive but is NOT a clauster.pty_keeper, so a sidecar pointing
    # at its PID (the recycled-PID case) must classify dead — never a live orphan.
    _sidecar(tmp_path, "ghost", keeper_pid=os.getpid())
    [info] = pty_keeper.iter_keepers(tmp_path)
    assert info.alive is False


def _keeper_argv():
    """The cmdline shape `is_keeper_cmdline` accepts, for a faked psutil.Process."""
    return (sys.executable, "-m", "clauster.pty_keeper", "--sidecar", "/tmp/k.json")


class _FakeKeeperProc:
    """A live process whose cmdline IS a keeper's, with a fixed create-time."""

    create_time_value = 1000.0

    def __init__(self, pid):
        pass

    def status(self):
        return procutil.psutil.STATUS_RUNNING

    def cmdline(self):
        return list(_keeper_argv())

    def create_time(self):
        return type(self).create_time_value


def test_is_live_keeper_rejects_a_different_keeper_on_the_same_pid(monkeypatch):
    # #1178, the whole point. `is_keeper_process` answers "is this pid *a* keeper", which a
    # DIFFERENT live keeper that inherited the pid also satisfies — and on a host running many
    # interactive sessions those are the pids most likely to be recycled by another keeper.
    # The persisted create-time is what tells the two apart.
    monkeypatch.setattr(procutil.psutil, "Process", _FakeKeeperProc)
    assert procutil.is_keeper_process(1234) is True  # ...a keeper, by cmdline
    # ...and ours: start times agree
    assert procutil.is_live_keeper(1234, 1000.0, start_ticks=None) is True
    # a keeper, but NOT the one we stored
    assert procutil.is_live_keeper(1234, 500.0, start_ticks=None) is False


def test_is_live_keeper_degrades_to_cmdline_when_the_start_time_is_unknown(monkeypatch):
    # Backward compatibility for a row written before `keeper_proc_start` existed: with no
    # stored start-time the answer must stay exactly what it was — cmdline + alive. Treating
    # unknown as a mismatch would report a live keeper as dead and let `forget` drop the
    # record of a running process.
    monkeypatch.setattr(procutil.psutil, "Process", _FakeKeeperProc)
    assert procutil.is_live_keeper(1234, None, start_ticks=None) is True


def test_is_live_keeper_survives_a_clock_step_when_the_ticks_match(monkeypatch):
    # THE #1402 regression test, driven by the clock offset directly rather than by waiting
    # on a drifting host. psutil derives create_time on Linux as `starttime/CLK_TCK +
    # boot_time()`, and boot_time() re-reads /proc/stat btime every call — so an NTP
    # correction moves the epoch of a keeper that never restarted. Here the stored epoch is
    # 1000.0 and the live one now reads 1004.0, four seconds out, against a 0.05s bound.
    monkeypatch.setattr(procutil.psutil, "Process", _FakeKeeperProc)
    monkeypatch.setattr(_FakeKeeperProc, "create_time_value", 1004.0)
    # The boot-relative half did not move, because nothing about the process did.
    monkeypatch.setattr(procutil, "proc_start_ticks", lambda pid: 770579)

    assert procutil.is_live_keeper(1234, 1000.0, start_ticks=770579) is True
    # The control that names the fault: with no recorded ticks the same keeper reads DEAD,
    # which is what let `forget` delete the row of a running keeper and its pty bridge.
    assert procutil.is_live_keeper(1234, 1000.0, start_ticks=None) is False


def test_is_live_keeper_rejects_a_keeper_whose_ticks_moved(monkeypatch):
    # The other direction, and why the tick compare is EXACT: a pid recycled onto a second
    # keeper differs by a whole CLK_TCK of ticks even when the epochs agree to the bit. The
    # drift fix must not cost the PID-reuse defense it exists beside.
    monkeypatch.setattr(procutil.psutil, "Process", _FakeKeeperProc)
    monkeypatch.setattr(_FakeKeeperProc, "create_time_value", 1000.0)
    monkeypatch.setattr(procutil, "proc_start_ticks", lambda pid: 770580)

    assert procutil.is_live_keeper(1234, 1000.0, start_ticks=770579) is False
    # ...and the epoch alone, which matches exactly here, would have admitted it.
    assert procutil.is_live_keeper(1234, 1000.0, start_ticks=None) is True


def test_is_live_keeper_falls_back_to_the_epoch_where_ticks_are_unreadable(monkeypatch):
    # macOS, Windows, and any unreadable /proc: `proc_start_ticks` answers None and the
    # recorded ticks have nothing to compare against. The answer must be the pre-#1402
    # epoch compare, not "dead" — those platforms record an absolute timestamp at exec and
    # never re-derive it, so their epochs do not drift in the first place.
    monkeypatch.setattr(procutil.psutil, "Process", _FakeKeeperProc)
    monkeypatch.setattr(_FakeKeeperProc, "create_time_value", 1000.0)
    monkeypatch.setattr(procutil, "proc_start_ticks", lambda pid: None)

    assert procutil.is_live_keeper(1234, 1000.0, start_ticks=770579) is True
    assert procutil.is_live_keeper(1234, 500.0, start_ticks=770579) is False


def test_is_live_keeper_rejects_a_live_non_keeper_pid():
    # The cmdline half still holds: a pid recycled onto a stranger is not a live keeper,
    # start-time or no start-time. Real process, real psutil — no fake to drift out of sync.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        ct = procutil.proc_create_time(proc.pid)
        assert ct is not None  # the stranger is genuinely alive
        assert procutil.is_live_keeper(proc.pid, ct, start_ticks=None) is False
        assert procutil.is_live_keeper(proc.pid, None, start_ticks=None) is False
    finally:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def test_is_live_keeper_fails_closed_on_a_dead_or_negative_pid():
    # Same untrusted on-disk ints the rest of the family absorbs: a sidecar or a hand-edited
    # state row can hold either, and neither may raise into `forget`.
    assert procutil.is_live_keeper(2_147_483_646, 1000.0, start_ticks=None) is False
    assert procutil.is_live_keeper(-1, None, start_ticks=None) is False


def test_stop_keeper_refuses_to_kill_a_non_keeper_pid():
    # A live process that is NOT our keeper (a PID recycled onto a stranger) must never
    # be SIGKILLed: stop_keeper reports it gone but leaves the stranger running.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        ct = procutil.proc_create_time(proc.pid)
        assert ct is not None  # the stranger is alive...
        assert (
            pty_keeper.stop_keeper(
                proc.pid, expect_create_time=ct, expect_start_ticks=None, expect_boot_id=None
            )
            is True
        )
        assert procutil.proc_create_time(proc.pid) is not None  # ...and was NOT killed
    finally:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
