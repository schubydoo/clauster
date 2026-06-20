"""Orphaned pty-keeper discovery + stop, and the `clauster keepers` CLI (#301)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from clauster import procutil, pty_keeper
from clauster.__main__ import main

_DEAD_PID = 2_147_483_646  # far above any real pid → proc_create_time is None


@pytest.fixture(autouse=True)
def _bypass_keeper_cmdline_gate(monkeypatch):
    """Treat any live PID as a keeper so these tests can stand in os.getpid() / a plain
    sleeper for a real ``clauster.pty_keeper``. They exercise liveness / orphan / kill
    logic; the cmdline gate itself is covered in test_keeper_cmdline_gate.py (RUNOPS-1)."""
    monkeypatch.setattr(procutil, "is_keeper_process", lambda pid: True)


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


def test_find_orphan_keepers_carded_glob_error_protects_by_name(tmp_path, monkeypatch):
    # iter_keepers succeeds (a live keeper for carded "alpha"), but alpha's per-project
    # glob raises → the carded keeper must still NOT surface as an orphan (fail-closed).
    _sidecar(tmp_path, "alpha", keeper_pid=os.getpid())
    real_glob = pty_keeper.Path.glob

    def _selective(self, pattern):
        if pattern.startswith("alpha-"):
            raise OSError("simulated unreadable")
        return real_glob(self, pattern)

    monkeypatch.setattr(pty_keeper.Path, "glob", _selective)
    assert pty_keeper.find_orphan_keepers(tmp_path, {"alpha"}) == []


# ----- stop ----------------------------------------------------------------


def test_stop_keeper_returns_true_when_already_gone(monkeypatch):
    monkeypatch.setattr(procutil, "proc_create_time", lambda pid: None)
    monkeypatch.setattr(procutil, "reap_if_exited", lambda pid: None)
    assert pty_keeper.stop_keeper(123) is True


def test_stop_keeper_force_kills_a_lingering_keeper(monkeypatch):
    alive = {"v": True}
    forced = {"n": 0}
    monkeypatch.setattr(procutil, "reap_if_exited", lambda pid: None)
    monkeypatch.setattr(pty_keeper.time, "sleep", lambda s: None)  # don't wait the real grace
    monkeypatch.setattr(procutil, "proc_create_time", lambda pid: 1.0 if alive["v"] else None)

    def _force(pid):
        forced["n"] += 1
        alive["v"] = False  # the hard kill succeeds

    monkeypatch.setattr(procutil, "force_kill_tree", _force)
    assert pty_keeper.stop_keeper(123) is True
    assert forced["n"] == 1  # grace expired → force path taken


def test_stop_keeper_returns_false_when_force_fails(monkeypatch):
    # A keeper that stays alive even through the force path → report failure honestly.
    monkeypatch.setattr(procutil, "reap_if_exited", lambda pid: None)
    monkeypatch.setattr(pty_keeper.time, "sleep", lambda s: None)
    monkeypatch.setattr(procutil, "proc_create_time", lambda pid: 1.0)  # never dies
    monkeypatch.setattr(procutil, "force_kill_tree", lambda pid: None)
    assert pty_keeper.stop_keeper(123) is False


def test_stop_keeper_refuses_on_pid_reuse(monkeypatch):
    # After the grace window the PID's create-time no longer matches what we classified
    # → the PID was recycled onto another process; refuse the kill.
    forced = {"n": 0}
    monkeypatch.setattr(procutil, "reap_if_exited", lambda pid: None)
    monkeypatch.setattr(pty_keeper.time, "sleep", lambda s: None)
    monkeypatch.setattr(procutil, "proc_create_time", lambda pid: 999.0)  # a stranger
    monkeypatch.setattr(procutil, "force_kill_tree", lambda pid: forced.__setitem__("n", 1))
    assert pty_keeper.stop_keeper(123, expect_create_time=100.0) is False
    assert forced["n"] == 0  # never SIGKILLed the reused PID


def test_stop_keeper_true_when_exits_at_reuse_guard(monkeypatch):
    # Alive through the grace loop, then gone exactly at the create-time re-check.
    seq = iter([1.0] * 8 + [None])

    def _no_force(pid):
        raise AssertionError("force_kill_tree must not run when the keeper already exited")

    monkeypatch.setattr(procutil, "reap_if_exited", lambda pid: None)
    monkeypatch.setattr(pty_keeper.time, "sleep", lambda s: None)
    monkeypatch.setattr(procutil, "proc_create_time", lambda pid: next(seq))
    monkeypatch.setattr(procutil, "force_kill_tree", _no_force)
    assert pty_keeper.stop_keeper(123, expect_create_time=1.0) is True


def test_stop_keeper_true_when_pid_reused_after_force(monkeypatch):
    # Matching through grace + the pre-force guard; force fires; then the PID is recycled
    # (create-time changes) in the post-force poll → report the kill as succeeded.
    times = iter([100.0] * 8 + [100.0, 999.0])  # grace(8) + pre-force guard + post-force
    monkeypatch.setattr(procutil, "reap_if_exited", lambda pid: None)
    monkeypatch.setattr(pty_keeper.time, "sleep", lambda s: None)
    monkeypatch.setattr(procutil, "proc_create_time", lambda pid: next(times))
    monkeypatch.setattr(procutil, "force_kill_tree", lambda pid: None)
    assert pty_keeper.stop_keeper(123, expect_create_time=100.0) is True


def test_stop_keeper_kills_a_real_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert procutil.proc_create_time(proc.pid) is not None
        assert pty_keeper.stop_keeper(proc.pid) is True
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
