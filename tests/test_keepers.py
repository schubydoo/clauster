"""Orphaned pty-keeper discovery + stop, and the `clauster keepers` CLI (#301)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from clauster import procutil, pty_keeper
from clauster.__main__ import main

_DEAD_PID = 2_147_483_646  # far above any real pid → proc_create_time is None


@pytest.fixture(autouse=True)
def _bypass_keeper_cmdline_gate(monkeypatch):
    """Treat any live PID as a keeper so these tests can stand in os.getpid() / a plain
    sleeper for a real ``clauster.pty_keeper``. They exercise liveness / orphan / kill
    logic; the cmdline gate itself is covered in test_keeper_cmdline_gate.py (RUNOPS-1).

    Only the CMDLINE half is bypassed — the liveness half is answered honestly, which is what
    "any LIVE pid" says. A blanket ``True`` also asserted a dead pid is a keeper, and that was
    invisible only while ``iter_keepers`` carried a separate ``create_time is not None``
    conjunct doing the liveness work. Since #1402 ``is_keeper_process`` IS that whole test, so
    a blanket stub would report every dead pid as a live orphan and hide the real behaviour.
    """
    monkeypatch.setattr(procutil, "is_keeper_process", lambda pid: not procutil.proc_is_gone(pid))


def _sidecar(log_dir: Path, name: str, *, keeper_pid: int, seq: int = 0, **fields) -> Path:
    """Write a `<name>-<ms>-<seq>.keeper.json` sidecar; return its path."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{name}-1700000000000-{seq}.keeper.json"
    payload = {"keeper_pid": keeper_pid, "bridge_pid": None, "session_id": None, "state": "ready"}
    payload.update(fields)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ----- discovery -----------------------------------------------------------


def test_project_parse_handles_hyphenated_names():
    assert pty_keeper._project_from_sidecar("alpha-1700000000000-0.keeper.json") == "alpha"
    assert pty_keeper._project_from_sidecar("a-b-c-1700000000000-3.keeper.json") == "a-b-c"
    assert pty_keeper._project_from_sidecar("not-a-keeper.txt") is None


def test_iter_keepers_reads_fields_and_liveness(tmp_path):
    _sidecar(tmp_path, "alpha", keeper_pid=os.getpid(), bridge_pid=4242, session_id="session_X")
    _sidecar(tmp_path, "beta", keeper_pid=_DEAD_PID, seq=1)
    by_project = {k.project: k for k in pty_keeper.iter_keepers(tmp_path)}
    assert by_project["alpha"].alive is True  # our own pid is live
    assert by_project["alpha"].bridge_pid == 4242
    assert by_project["alpha"].session_id == "session_X"
    assert by_project["beta"].alive is False  # dead pid


def test_iter_keepers_reads_the_boot_id_and_stays_none_when_absent(tmp_path):
    # #1401: the sidecar's recorded boot id is carried onto KeeperInfo so stop_keeper can
    # reject a cross-boot recycled pid on identity. A pre-#1401 sidecar (no boot_id) loads None.
    _sidecar(tmp_path, "alpha", keeper_pid=os.getpid(), boot_id="boot-uuid-1")
    _sidecar(tmp_path, "beta", keeper_pid=os.getpid(), seq=1)  # no boot_id key
    by_project = {k.project: k for k in pty_keeper.iter_keepers(tmp_path)}
    assert by_project["alpha"].keeper_boot_id == "boot-uuid-1"
    assert by_project["beta"].keeper_boot_id is None


def test_iter_keepers_lists_a_live_keeper_on_a_btime_less_procfs(tmp_path, monkeypatch):
    # gVisor / WSL1 (#1402): `proc_start_pair` answers `(None, ticks)` there, because the
    # epoch needs `boot_time()` and the ticks do not. `alive` used to conjoin
    # `create_time is not None`, so it read False for a keeper that is plainly running — and
    # `find_orphan_keepers` filters on `alive`, so `clauster keepers` listed nothing and
    # `--kill` refused every pid. `is_keeper_process` alone already proves live + non-zombie
    # + keeper cmdline, which is what `alive` means.
    _sidecar(tmp_path, "alpha", keeper_pid=os.getpid(), bridge_pid=4242)
    monkeypatch.setattr(procutil, "proc_start_pair", lambda pid: (None, 770579))

    [info] = pty_keeper.iter_keepers(tmp_path)

    assert info.alive is True, "a keeper with no readable epoch was hidden from the CLI"
    assert (info.keeper_create_time, info.keeper_start_ticks) == (None, 770579)
    assert pty_keeper.find_orphan_keepers(tmp_path, carded_projects=set()) == [info]


def test_iter_keepers_still_rejects_a_pid_that_is_not_a_keeper(tmp_path, monkeypatch):
    # The control for the test above: dropping the create-time conjunct must not widen what
    # counts as live. The cmdline gate is the whole defense now, so it has to still bite.
    _sidecar(tmp_path, "alpha", keeper_pid=os.getpid())
    monkeypatch.setattr(procutil, "is_keeper_process", lambda pid: False)  # beats the autouse

    [info] = pty_keeper.iter_keepers(tmp_path)

    assert info.alive is False


def test_find_orphan_keepers_excludes_carded_and_dead(tmp_path):
    _sidecar(tmp_path, "carded", keeper_pid=os.getpid())  # live, but on a card
    _sidecar(tmp_path, "ghost", keeper_pid=os.getpid(), seq=1)  # live, no card → orphan
    _sidecar(tmp_path, "deadghost", keeper_pid=_DEAD_PID, seq=2)  # no card but dead → not orphan
    orphans = pty_keeper.find_orphan_keepers(tmp_path, carded_projects={"carded"})
    assert [o.project for o in orphans] == ["ghost"]


def test_find_orphan_keepers_empty_when_no_log_dir(tmp_path):
    assert pty_keeper.find_orphan_keepers(tmp_path / "nope", carded_projects=set()) == []


def test_iter_keepers_tolerates_corrupt_sidecar(tmp_path):
    (tmp_path / "bad-1700000000000-0.keeper.json").write_text("{ not json", encoding="utf-8")
    [info] = pty_keeper.iter_keepers(tmp_path)
    assert info.project == "bad" and info.keeper_pid is None and info.alive is False


@pytest.mark.parametrize("bad", [True, False])
def test_iter_keepers_rejects_bool_pids(tmp_path, bad):
    # bool is an int subclass; a corrupt sidecar must not resolve True → PID 1 or
    # False → PID 0 (the kernel swapper), both unsafe to target (#472).
    _sidecar(tmp_path, "bools", keeper_pid=bad, bridge_pid=bad)
    [info] = pty_keeper.iter_keepers(tmp_path)
    assert info.keeper_pid is None and info.bridge_pid is None and info.alive is False


def test_iter_keepers_tolerates_glob_error(tmp_path, monkeypatch):
    def _boom(self, pattern):
        raise OSError("unreadable")

    monkeypatch.setattr(pty_keeper.Path, "glob", _boom)
    assert pty_keeper.iter_keepers(tmp_path) == []


def test_find_orphan_keepers_tolerates_glob_error(tmp_path, monkeypatch):
    def _boom(self, pattern):
        raise OSError("unreadable")

    monkeypatch.setattr(pty_keeper.Path, "glob", _boom)
    assert pty_keeper.find_orphan_keepers(tmp_path, {"alpha"}) == []


def test_find_orphan_keepers_anchors_on_stem_not_prefix(tmp_path):
    # #1181: protection keys on the parsed <name> stem, not an unanchored "app-*" prefix.
    # With "app" carded and its sibling "app-staging" removed, the removed project's live
    # keeper must surface as the orphan it is — the old glob("app-*") hid it (and --kill
    # refused it) for as long as "app" stayed carded.
    _sidecar(tmp_path, "app", keeper_pid=os.getpid())  # carded, live → protected
    _sidecar(tmp_path, "app-staging", keeper_pid=os.getpid(), seq=1)  # removed, live → orphan
    orphans = pty_keeper.find_orphan_keepers(tmp_path, carded_projects={"app"})
    assert [o.project for o in orphans] == ["app-staging"]


def test_find_orphan_keepers_unparsable_name_is_orphan(tmp_path):
    # A sidecar whose filename doesn't parse to a <name>-<ms>-<seq> stem (project is None)
    # belongs to no card, so a live one is an orphan — same result the old glob gave it.
    (tmp_path / "weirdname.keeper.json").write_text(
        json.dumps({"keeper_pid": os.getpid(), "state": "ready"}), encoding="utf-8"
    )
    orphans = pty_keeper.find_orphan_keepers(tmp_path, carded_projects={"app"})
    assert [o.project for o in orphans] == [None]


# ----- stop ----------------------------------------------------------------


def _pin_start(monkeypatch, *, epoch, ticks=lambda: None) -> None:
    """Pin every start-identity reader `stop_keeper` consults, from one fiction.

    `epoch`/`ticks` are zero-arg callables so a test can move either half mid-run. Pinned
    together because the grace probe (`proc_is_gone`), the identity read (`proc_start_pair`)
    and the legacy epoch reader all describe the SAME process — letting real psutil answer
    any one of them for pid 123 would let the host decide the test. `proc_create_time` is in
    the set even though `stop_keeper` no longer calls it: the fiction has to stay consistent
    for a reader (and for any future caller) rather than leave one reader live.
    """
    monkeypatch.setattr(procutil, "proc_create_time", lambda pid: epoch())
    monkeypatch.setattr(procutil, "proc_start_ticks", lambda pid: ticks())
    monkeypatch.setattr(procutil, "proc_start_pair", lambda pid: (epoch(), ticks()))
    monkeypatch.setattr(procutil, "proc_is_gone", lambda pid: epoch() is None and ticks() is None)


def test_stop_keeper_returns_true_when_already_gone(monkeypatch):
    _pin_start(monkeypatch, epoch=lambda: None)
    monkeypatch.setattr(procutil, "reap_if_exited", lambda pid: None)
    assert pty_keeper.stop_keeper(123, expect_start_ticks=None, expect_boot_id=None) is True


def test_stop_keeper_force_kills_a_lingering_keeper(monkeypatch):
    alive = {"v": True}
    forced = {"n": 0}
    monkeypatch.setattr(procutil, "reap_if_exited", lambda pid: None)
    monkeypatch.setattr(pty_keeper.time, "sleep", lambda s: None)  # don't wait the real grace
    _pin_start(monkeypatch, epoch=lambda: 1.0 if alive["v"] else None)

    def _force(pid):
        forced["n"] += 1
        alive["v"] = False  # the hard kill succeeds

    monkeypatch.setattr(procutil, "force_kill_tree", _force)
    assert pty_keeper.stop_keeper(123, expect_start_ticks=None, expect_boot_id=None) is True
    assert forced["n"] == 1  # grace expired → force path taken


def test_stop_keeper_winds_down_a_keeper_on_a_btime_less_procfs(monkeypatch):
    # gVisor / WSL1 (#1402): psutil's create_time ends in `+ boot_time()`, which raises on a
    # procfs with no btime, so the epoch is unavailable for a keeper that is plainly running
    # while `/proc/<pid>/stat` field 22 reads fine. The grace loop used to call that "gone"
    # and return True without killing anything — a lingering keeper and its pty bridge left
    # running, silently, and nothing logged. The boot-relative half proves it is still there.
    alive = {"v": True}
    forced = {"n": 0}
    monkeypatch.setattr(procutil, "reap_if_exited", lambda pid: None)
    monkeypatch.setattr(pty_keeper.time, "sleep", lambda s: None)
    _pin_start(monkeypatch, epoch=lambda: None, ticks=lambda: 4200 if alive["v"] else None)

    def _force(pid):
        forced["n"] += 1
        alive["v"] = False

    monkeypatch.setattr(procutil, "force_kill_tree", _force)
    assert (
        pty_keeper.stop_keeper(
            123, expect_create_time=None, expect_start_ticks=4200, expect_boot_id=None
        )
        is True
    )
    assert forced["n"] == 1, "a keeper with no readable epoch was never wound down"


def test_stop_keeper_kills_the_root_when_the_tree_cannot_read_a_clock(monkeypatch):
    # Same host as above, through the REAL force_kill_tree: psutil's children() reads the
    # epoch on its own, so it raises there even on 7.1+, and `clauster keepers --kill` ended
    # in a traceback instead of a kill. The root still dies; only its tree is unreadable.
    alive = {"v": True}
    killed: list[int] = []
    monkeypatch.setattr(procutil, "reap_if_exited", lambda pid: None)
    monkeypatch.setattr(pty_keeper.time, "sleep", lambda s: None)
    _pin_start(monkeypatch, epoch=lambda: None, ticks=lambda: 4200 if alive["v"] else None)

    class _KeeperWithoutATree:
        def __init__(self, pid):
            self.pid = pid

        def status(self):
            return psutil.STATUS_RUNNING

        def cmdline(self):
            return [sys.executable, "-m", "clauster.pty_keeper", "--sidecar", "/tmp/k.json"]

        def children(self, recursive=False):
            raise RuntimeError("no btime line in /proc/stat")

        def kill(self):
            killed.append(self.pid)
            alive["v"] = False

    monkeypatch.setattr(procutil.psutil, "Process", _KeeperWithoutATree)
    assert (
        pty_keeper.stop_keeper(
            123, expect_create_time=None, expect_start_ticks=4200, expect_boot_id=None
        )
        is True
    )
    assert killed == [123]


def test_stop_keeper_returns_false_when_force_fails(monkeypatch):
    # A keeper that stays alive even through the force path → report failure honestly.
    monkeypatch.setattr(procutil, "reap_if_exited", lambda pid: None)
    monkeypatch.setattr(pty_keeper.time, "sleep", lambda s: None)
    _pin_start(monkeypatch, epoch=lambda: 1.0)  # never dies
    monkeypatch.setattr(procutil, "force_kill_tree", lambda pid: None)
    assert pty_keeper.stop_keeper(123, expect_start_ticks=None, expect_boot_id=None) is False


def test_stop_keeper_refuses_on_pid_reuse(monkeypatch):
    # After the grace window the PID's create-time no longer matches what we classified
    # → the PID was recycled onto another process; refuse the kill.
    forced = {"n": 0}
    monkeypatch.setattr(procutil, "reap_if_exited", lambda pid: None)
    monkeypatch.setattr(pty_keeper.time, "sleep", lambda s: None)
    _pin_start(monkeypatch, epoch=lambda: 999.0)  # a stranger
    monkeypatch.setattr(procutil, "force_kill_tree", lambda pid: forced.__setitem__("n", 1))
    assert (
        pty_keeper.stop_keeper(
            123, expect_create_time=100.0, expect_start_ticks=None, expect_boot_id=None
        )
        is False
    )
    assert forced["n"] == 0  # never SIGKILLed the reused PID


def test_stop_keeper_refuses_a_pid_recycled_inside_its_own_grace(monkeypatch):
    # THE reason this guard needed the boot-relative half (#1402). `_KEEPER_START_TOLERANCE`
    # is 2.0s and cannot be tightened while the epoch stands alone, because psutil re-derives
    # that epoch from a btime NTP moves — so it is wide enough to admit a pid recycled during
    # stop_keeper's own ~2s grace, and what follows is a SIGKILL on a whole process tree. The
    # replacement keeper here starts 0.5s later, well inside that bound; its tick count
    # differs by a whole CLK_TCK and the exact compare rejects it.
    forced = {"n": 0}
    state = {"epoch": 100.0, "ticks": 4200, "sleeps": 0}

    def _sleep(_s):
        state["sleeps"] += 1
        if state["sleeps"] == 8:  # the original keeper exits and the pid is recycled
            state["epoch"], state["ticks"] = 100.5, 4250

    monkeypatch.setattr(procutil, "reap_if_exited", lambda pid: None)
    monkeypatch.setattr(pty_keeper.time, "sleep", _sleep)
    _pin_start(monkeypatch, epoch=lambda: state["epoch"], ticks=lambda: state["ticks"])
    # The module's autouse `_bypass_keeper_cmdline_gate` already answers "a keeper" for any
    # pid, which is the shape under test: a keeper by cmdline, just not OURS.
    monkeypatch.setattr(procutil, "force_kill_tree", lambda pid: forced.__setitem__("n", 1))

    assert (
        pty_keeper.stop_keeper(
            123, expect_create_time=100.0, expect_start_ticks=4200, expect_boot_id=None
        )
        is False
    )
    assert forced["n"] == 0, "force-killed the tree of a stranger the 2.0s epoch bound admitted"


def test_stop_keeper_kills_through_a_clock_step_when_the_ticks_hold(monkeypatch):
    # The other direction, and the control for the test above. The clock is corrected by four
    # seconds during the grace — past the 2.0s bound — but the keeper never moved, so its
    # boot-relative start is unchanged and the wind-down must still happen. Epoch-only, this
    # refused and the orphan leaked.
    alive = {"v": True}
    forced = {"n": 0}
    state = {"epoch": 100.0, "sleeps": 0}

    def _sleep(_s):
        state["sleeps"] += 1
        if state["sleeps"] == 8:
            state["epoch"] = 104.0  # NTP steps the clock; btime moved under a live process

    monkeypatch.setattr(procutil, "reap_if_exited", lambda pid: None)
    monkeypatch.setattr(pty_keeper.time, "sleep", _sleep)
    _pin_start(
        monkeypatch,
        epoch=lambda: state["epoch"] if alive["v"] else None,
        ticks=lambda: 4200 if alive["v"] else None,
    )

    def _force(pid):
        forced["n"] += 1
        alive["v"] = False

    monkeypatch.setattr(procutil, "force_kill_tree", _force)
    assert (
        pty_keeper.stop_keeper(
            123, expect_create_time=100.0, expect_start_ticks=4200, expect_boot_id=None
        )
        is True
    )
    assert forced["n"] == 1, "clock drift, not a pid recycle, spared a keeper that never moved"


def test_stop_keeper_refuses_a_sidecar_from_an_earlier_boot_on_the_boot_id(monkeypatch):
    # #1401: iter_keepers reads the keeper's ticks/epoch LIVE, so a sidecar that survived a
    # reboot onto a recycled pid matches them exactly — the tick conjunct alone would SIGKILL
    # the stranger. The sidecar's recorded boot id is the ORIGINAL boot's; a mismatch with the
    # live one rejects it on identity, immune to the wall clock. Without the boot id the same
    # setup killed the stranger, which is what this closes.
    forced = {"n": 0}
    monkeypatch.setattr(procutil, "reap_if_exited", lambda pid: None)
    monkeypatch.setattr(pty_keeper.time, "sleep", lambda s: None)
    _pin_start(monkeypatch, epoch=lambda: 100.0, ticks=lambda: 4200)  # matches expect exactly
    monkeypatch.setattr(procutil, "proc_boot_id", lambda: "boot-N+1")  # the current boot
    monkeypatch.setattr(procutil, "force_kill_tree", lambda pid: forced.__setitem__("n", 1))
    assert (
        pty_keeper.stop_keeper(
            123, expect_create_time=100.0, expect_start_ticks=4200, expect_boot_id="boot-N"
        )
        is False
    )
    assert forced["n"] == 0, "force-killed a stranger the tick match admitted across a reboot"

    # Control: a MATCHING boot id lets the genuine same-boot wind-down proceed.
    alive = {"v": True}
    _pin_start(
        monkeypatch,
        epoch=lambda: 100.0 if alive["v"] else None,
        ticks=lambda: 4200 if alive["v"] else None,
    )

    def _force(pid):
        forced["n"] += 1
        alive["v"] = False

    monkeypatch.setattr(procutil, "force_kill_tree", _force)
    assert (
        pty_keeper.stop_keeper(
            123, expect_create_time=100.0, expect_start_ticks=4200, expect_boot_id="boot-N+1"
        )
        is True
    )
    assert forced["n"] == 1, "a matching boot id must not block a genuine wind-down"


def test_stop_keeper_refuses_when_an_expectation_cannot_be_checked(monkeypatch):
    # Fail closed on "could not tell", not just on "definitely different". A recorded
    # `expect_create_time` with an unreadable epoch and no recorded ticks used to read
    # `proc_create_time is None` as "exited during the grace" and return True — a false
    # success with no kill. False is the honest answer for an unproven identity in front of
    # a SIGKILL on a whole process tree. The ticks read here belong to whatever holds the
    # pid; nothing was recorded to compare them against.
    forced = {"n": 0}
    monkeypatch.setattr(procutil, "reap_if_exited", lambda pid: None)
    monkeypatch.setattr(pty_keeper.time, "sleep", lambda s: None)
    _pin_start(monkeypatch, epoch=lambda: None, ticks=lambda: 4200)
    monkeypatch.setattr(procutil, "force_kill_tree", lambda pid: forced.__setitem__("n", 1))

    assert (
        pty_keeper.stop_keeper(
            123, expect_create_time=100.0, expect_start_ticks=None, expect_boot_id=None
        )
        is False
    )
    assert forced["n"] == 0, "an unproven identity must never front a force_kill_tree"


def test_stop_keeper_does_not_report_success_from_an_unreadable_post_kill_poll(monkeypatch):
    # The other side of the same split. After the SIGKILL the poll may only treat a DEFINITE
    # mismatch as proof the keeper is gone: an unreadable pair means "could not tell", and
    # reporting the kill as succeeded there would turn an unconfirmed kill into a silent
    # success. Waiting the poll out and reporting False is the honest answer.
    reads = {"n": 0}

    def _epoch():
        reads["n"] += 1
        return 100.0 if reads["n"] <= 9 else None  # grace(8) + pre-force guard, then blind

    monkeypatch.setattr(procutil, "reap_if_exited", lambda pid: None)
    monkeypatch.setattr(pty_keeper.time, "sleep", lambda s: None)
    # Ticks stay readable throughout, so `proc_is_gone` never fires — only the recorded-vs-
    # observed comparability changes, which is exactly the branch under test.
    _pin_start(monkeypatch, epoch=_epoch, ticks=lambda: 4200)
    monkeypatch.setattr(procutil, "force_kill_tree", lambda pid: None)

    assert (
        pty_keeper.stop_keeper(
            123, expect_create_time=100.0, expect_start_ticks=None, expect_boot_id=None
        )
        is False
    )


def test_stop_keeper_true_when_exits_at_reuse_guard(monkeypatch):
    # Alive through the grace loop, then gone exactly at the create-time re-check.
    seq = iter([1.0] * 8 + [None])

    def _no_force(pid):
        raise AssertionError("force_kill_tree must not run when the keeper already exited")

    monkeypatch.setattr(procutil, "reap_if_exited", lambda pid: None)
    monkeypatch.setattr(pty_keeper.time, "sleep", lambda s: None)
    _pin_start(monkeypatch, epoch=lambda: next(seq, None))
    monkeypatch.setattr(procutil, "force_kill_tree", _no_force)
    assert (
        pty_keeper.stop_keeper(
            123, expect_create_time=1.0, expect_start_ticks=None, expect_boot_id=None
        )
        is True
    )


def test_stop_keeper_true_when_pid_reused_after_force(monkeypatch):
    # Matching through grace + the pre-force guard; force fires; then the PID is recycled
    # (create-time changes) in the post-force poll → report the kill as succeeded.
    times = iter([100.0] * 8 + [100.0])  # grace(8) + pre-force guard; then the stranger
    monkeypatch.setattr(procutil, "reap_if_exited", lambda pid: None)
    monkeypatch.setattr(pty_keeper.time, "sleep", lambda s: None)
    _pin_start(monkeypatch, epoch=lambda: next(times, 999.0))
    monkeypatch.setattr(procutil, "force_kill_tree", lambda pid: None)
    assert (
        pty_keeper.stop_keeper(
            123, expect_create_time=100.0, expect_start_ticks=None, expect_boot_id=None
        )
        is True
    )


def test_stop_keeper_kills_a_real_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert procutil.proc_create_time(proc.pid) is not None
        assert (
            pty_keeper.stop_keeper(proc.pid, expect_start_ticks=None, expect_boot_id=None) is True
        )
        assert procutil.proc_create_time(proc.pid) is None
    finally:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


# ----- the `clauster keepers` CLI ------------------------------------------


def _config(tmp_path: Path) -> Path:
    """A minimal clauster.yml; return its path. The log dir is state_dir/logs."""
    (tmp_path / "projects").mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "clauster.yml"
    cfg.write_text(
        f"projects_root: {tmp_path / 'projects'}\nstate_dir: {tmp_path / 'state'}\n",
        encoding="utf-8",
    )
    return cfg


def test_cli_list_reports_orphans(tmp_path, capsys):
    cfg = _config(tmp_path)
    _sidecar(tmp_path / "state" / "logs", "ghost", keeper_pid=os.getpid())
    rc = main(["keepers", "-c", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "1 orphaned keeper" in out and "ghost" in out


def test_cli_list_empty(tmp_path, capsys):
    cfg = _config(tmp_path)
    rc = main(["keepers", "-c", str(cfg)])
    assert rc == 0 and "no orphaned keepers" in capsys.readouterr().out


def test_cli_kill_refuses_unknown_pid(tmp_path, capsys):
    cfg = _config(tmp_path)
    _sidecar(tmp_path / "state" / "logs", "ghost", keeper_pid=os.getpid())
    rc = main(["keepers", "-c", str(cfg), "--kill", str(_DEAD_PID)])
    assert rc == 2  # not an orphan (dead) → refused
    assert "refusing to kill" in capsys.readouterr().err


def test_cli_kill_refuses_carded_keeper(tmp_path, capsys):
    cfg = _config(tmp_path)
    # A live keeper whose project IS a card → not an orphan; --kill must refuse it.
    from clauster.state import StateStore

    StateStore(tmp_path / "state").save({"alpha": {"label": "alpha"}})
    _sidecar(tmp_path / "state" / "logs", "alpha", keeper_pid=os.getpid())
    rc = main(["keepers", "-c", str(cfg), "--kill", str(os.getpid())])
    assert rc == 2 and "refusing to kill" in capsys.readouterr().err


def test_cli_kill_refuses_db_carded_keeper_after_migration(tmp_path, capsys):
    # Post JSON->DB migration the flat state.json is gone (renamed *.imported), so the
    # card set must come from the DB. A keeper carded in the DB — with NO flat
    # state.json present — must still be refused, not mislabeled an orphan and reaped.
    cfg = _config(tmp_path)
    from clauster.db.persistence import Persistence

    persistence = Persistence(tmp_path / "state")
    try:
        persistence.state_store().save(
            {
                "aaaaaaaa-0000-0000-0000-000000000001": {
                    "project_name": "alpha",
                    "label": "alpha",
                }
            }
        )
    finally:
        persistence.dispose()
    assert not (tmp_path / "state" / "state.json").exists()  # DB-only; no flat card file
    _sidecar(tmp_path / "state" / "logs", "alpha", keeper_pid=os.getpid())
    rc = main(["keepers", "-c", str(cfg), "--kill", str(os.getpid())])
    assert rc == 2 and "refusing to kill" in capsys.readouterr().err


def test_cli_kill_reports_failure(tmp_path, capsys, monkeypatch):
    cfg = _config(tmp_path)
    _sidecar(tmp_path / "state" / "logs", "ghost", keeper_pid=os.getpid())
    monkeypatch.setattr(pty_keeper, "stop_keeper", lambda pid, **kw: False)  # never kills
    rc = main(["keepers", "-c", str(cfg), "--kill", str(os.getpid())])
    assert rc == 1 and "failed to stop" in capsys.readouterr().err


def test_cli_kill_tolerates_missing_sidecar_on_cleanup(tmp_path, capsys, monkeypatch):
    cfg = _config(tmp_path)
    sc = _sidecar(tmp_path / "state" / "logs", "ghost", keeper_pid=os.getpid())

    def _stop(pid, **kw):
        sc.unlink()  # the sidecar is already gone when _keepers tries to remove it
        return True

    monkeypatch.setattr(pty_keeper, "stop_keeper", _stop)
    rc = main(["keepers", "-c", str(cfg), "--kill", str(os.getpid())])
    assert rc == 0 and "stopped orphaned keeper" in capsys.readouterr().out


def test_cli_kill_stops_an_orphan(tmp_path, capsys):
    cfg = _config(tmp_path)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    sidecar = _sidecar(tmp_path / "state" / "logs", "ghost", keeper_pid=proc.pid)
    try:
        rc = main(["keepers", "-c", str(cfg), "--kill", str(proc.pid)])
        assert rc == 0
        assert "stopped orphaned keeper" in capsys.readouterr().out
        assert procutil.proc_create_time(proc.pid) is None  # actually gone
        assert not sidecar.exists()  # stale sidecar removed
    finally:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def test_cli_force_kill_stops_a_carded_keeper(tmp_path, capsys):
    # The recovery path (#1420): a keeper `_cleanup_keeper` spared on a still-carded project
    # is refused by the orphan sweep (positive control below), but `--force` reaches it after
    # the same PID-reuse re-verify. Without this the keeper is unreachable by every automated
    # path and can only be killed by hand.
    from clauster.state import StateStore

    cfg = _config(tmp_path)
    # The keeper's project ("alpha") is a current card, so the orphan sweep hides it.
    StateStore(tmp_path / "state").save({"alpha": {"label": "alpha"}})
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    sidecar = _sidecar(tmp_path / "state" / "logs", "alpha", keeper_pid=proc.pid)
    try:
        assert main(["keepers", "-c", str(cfg), "--kill", str(proc.pid)]) == 2  # carded → refused
        capsys.readouterr()
        rc = main(["keepers", "-c", str(cfg), "--kill", str(proc.pid), "--force"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "stopped keeper" in out and "alpha" in out
        assert procutil.proc_create_time(proc.pid) is None  # actually gone
        assert not sidecar.exists()  # stale sidecar removed
    finally:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def test_cli_force_kill_still_refuses_a_dead_pid(tmp_path, capsys):
    # `--force` overrides the orphan filter, NOT the liveness gate: a dead pid has nothing to
    # kill and must still be refused rather than acted on.
    cfg = _config(tmp_path)
    _sidecar(tmp_path / "state" / "logs", "alpha", keeper_pid=_DEAD_PID)
    rc = main(["keepers", "-c", str(cfg), "--kill", str(_DEAD_PID), "--force"])
    assert rc == 2 and "refusing to kill" in capsys.readouterr().err


def test_cli_force_without_kill_is_refused(tmp_path, capsys):
    cfg = _config(tmp_path)
    rc = main(["keepers", "-c", str(cfg), "--force"])
    assert rc == 2 and "--force applies only with --kill" in capsys.readouterr().err


def test_cli_force_kill_reports_failure_when_gate_refuses(tmp_path, capsys, monkeypatch):
    # The SIGKILL is still gated by stop_keeper's PID-reuse re-verify; a refusal surfaces as a
    # failure (exit 1), never a silent success.
    cfg = _config(tmp_path)
    _sidecar(tmp_path / "state" / "logs", "alpha", keeper_pid=os.getpid())
    monkeypatch.setattr(pty_keeper, "stop_keeper", lambda pid, **kw: False)
    rc = main(["keepers", "-c", str(cfg), "--kill", str(os.getpid()), "--force"])
    assert rc == 1 and "failed to stop" in capsys.readouterr().err
