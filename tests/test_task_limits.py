"""Task-limit counters: cgroup reads, the crash reason, the poll wiring, and doctor."""

from __future__ import annotations

import subprocess
import sys

import pytest

from clauster import bridge_scope, ops, task_limits
from clauster.models import InstanceStatus, RemoteControlInstance
from clauster.record_facade import RecordFacade
from clauster.runner import SessionRunner


@pytest.fixture
def roots(monkeypatch, tmp_path):
    cgroup = tmp_path / "cg"
    proc = tmp_path / "proc"
    cgroup.mkdir()
    proc.mkdir()
    monkeypatch.setattr(task_limits, "_CGROUP_ROOT", cgroup)
    monkeypatch.setattr(task_limits, "_PROC_ROOT", proc)
    return cgroup, proc


def _group(root, relative, refused):
    path = root / relative
    path.mkdir(parents=True, exist_ok=True)
    (path / "pids.events").write_text(f"max {refused}\n", encoding="ascii")
    return path


def _process(proc, pid, relative):
    (proc / str(pid)).mkdir()
    (proc / str(pid) / "cgroup").write_text(f"0::/{relative}\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("text", "expected"),
    [("max 7\n", 7), ("max 0\n", 0), ("other 3\n", None), ("max x\n", None), ("", None)],
)
def test_refused_forks_parses_the_counter(tmp_path, text, expected):
    (tmp_path / "pids.events").write_text(text, encoding="ascii")
    assert task_limits.refused_forks(tmp_path) == expected


def test_refused_forks_missing_file(tmp_path):
    assert task_limits.refused_forks(tmp_path) is None


def test_refused_forks_undecodable_file(tmp_path):
    (tmp_path / "pids.events").write_bytes(b"max \xff\n")
    assert task_limits.refused_forks(tmp_path) is None


@pytest.mark.parametrize("relative", ["", "no/leading/slash", "/a/../../etc", "/.."])
def test_cgroup_path_refuses_a_path_outside_the_root(relative):
    assert task_limits.cgroup_path(relative) is None


def test_cgroup_of_reads_the_v2_line(roots):
    cgroup, proc = roots
    (proc / "5").mkdir()
    (proc / "5" / "cgroup").write_text("1:name=x:/old\n0::/a/b\n", encoding="utf-8")
    assert task_limits.cgroup_of(5) == cgroup / "a" / "b"


def test_cgroup_of_without_a_v2_line_or_process(roots):
    _cgroup, proc = roots
    (proc / "6").mkdir()
    (proc / "6" / "cgroup").write_text("1:name=x:/old\n", encoding="utf-8")
    assert task_limits.cgroup_of(6) is None
    assert task_limits.cgroup_of(7) is None


def test_snapshot_records_own_and_parent(roots):
    cgroup, proc = roots
    _group(cgroup, "slice", 4)
    path = _group(cgroup, "slice/scope", 1)
    _process(proc, 9, "slice/scope")
    assert task_limits.snapshot(9) == task_limits.TaskEventCount(9, path, 1, 4)


def test_snapshot_without_data(roots):
    cgroup, proc = roots
    (cgroup / "bare").mkdir()
    _process(proc, 9, "bare")
    assert task_limits.snapshot(9) is None
    assert task_limits.snapshot(10) is None


def test_crash_note_when_the_own_counter_rose(roots):
    cgroup, _proc = roots
    path = _group(cgroup, "s/own", 5)
    base = task_limits.TaskEventCount(1, path, 2, None)
    note = task_limits.crash_note(base)
    assert note is not None and "refused 3 new process or thread starts" in note
    assert "likely" in note


def test_crash_note_singular(roots):
    cgroup, _proc = roots
    path = _group(cgroup, "s/own", 3)
    note = task_limits.crash_note(task_limits.TaskEventCount(1, path, 2, None))
    assert note is not None and "thread start shortly" in note


@pytest.mark.parametrize("now", [2, 1])
def test_crash_note_silent_without_a_rise(roots, now):
    cgroup, _proc = roots
    path = _group(cgroup, "s/own", now)
    assert task_limits.crash_note(task_limits.TaskEventCount(1, path, 2, None)) is None


def test_crash_note_uses_the_parent_when_the_scope_is_gone(roots):
    cgroup, _proc = roots
    _group(cgroup, "slice", 6)
    gone = cgroup / "slice" / "gone.scope"
    note = task_limits.crash_note(task_limits.TaskEventCount(1, gone, 0, 4))
    assert note is not None and "refused 2 new" in note
    assert "possible cause" in note and "likely" not in note


def test_crash_note_no_evidence_when_scope_and_baseline_are_gone(roots):
    cgroup, _proc = roots
    _group(cgroup, "slice", 6)
    gone = cgroup / "slice" / "gone.scope"
    assert task_limits.crash_note(task_limits.TaskEventCount(1, gone, 0, None)) is None


def test_crash_note_no_evidence_when_the_parent_is_unreadable(roots):
    cgroup, _proc = roots
    gone = cgroup / "missing" / "gone.scope"
    assert task_limits.crash_note(task_limits.TaskEventCount(1, gone, 0, 4)) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX uid")
def test_bridge_slice_refused_reads_the_user_slice(roots, monkeypatch):
    cgroup, _proc = roots
    monkeypatch.setattr(bridge_scope, "_uid", lambda: 1000)
    _group(
        cgroup,
        f"user.slice/user-1000.slice/user@1000.service/{bridge_scope.BRIDGE_SLICE}",
        8,
    )
    assert task_limits.bridge_slice_refused() == 8


def _crashing(runner_config, monkeypatch, pid=4242):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.RUNNING)
    inst.bridge_pid = pid
    runner._instances[inst.instance_id] = inst
    monkeypatch.setattr("clauster.runner.procutil.reap_if_exited", lambda *a, **k: None)
    monkeypatch.setattr("clauster.poll_loop.inspector.list_working_sessions", lambda *a, **k: [])
    monkeypatch.setattr(runner._record, "_emit_lifecycle", lambda *a, **k: None)
    return runner, inst


def _live(monkeypatch, value):
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: value)


async def test_poll_records_the_reason_when_a_limit_was_hit(runner_config, monkeypatch, roots):
    cgroup, proc = roots
    runner, inst = _crashing(runner_config, monkeypatch)
    path = _group(cgroup, "svc", 10)
    _process(proc, inst.bridge_pid, "svc")
    _live(monkeypatch, True)
    await runner.poll_once()
    assert inst.task_limit_note is None
    _group(cgroup, "svc", 13)
    assert path.exists()
    _live(monkeypatch, False)
    await runner.poll_once()
    assert inst.status is InstanceStatus.CRASHED
    assert inst.task_limit_note is not None and "refused 3" in inst.task_limit_note


async def test_poll_records_no_reason_without_a_rise(runner_config, monkeypatch, roots):
    cgroup, proc = roots
    runner, inst = _crashing(runner_config, monkeypatch)
    _group(cgroup, "svc", 10)
    _process(proc, inst.bridge_pid, "svc")
    _live(monkeypatch, True)
    await runner.poll_once()
    _live(monkeypatch, False)
    await runner.poll_once()
    assert inst.status is InstanceStatus.CRASHED
    assert inst.task_limit_note is None


async def test_poll_ignores_a_baseline_of_another_pid(runner_config, monkeypatch, roots):
    cgroup, _proc = roots
    runner, inst = _crashing(runner_config, monkeypatch)
    path = _group(cgroup, "svc", 13)
    runner._poll_loop._task_counts[inst.instance_id] = task_limits.TaskEventCount(1, path, 0, None)
    _live(monkeypatch, False)
    await runner.poll_once()
    assert inst.status is InstanceStatus.CRASHED
    assert inst.task_limit_note is None


async def test_second_crash_without_evidence_clears_the_old_note(
    runner_config, monkeypatch, roots
):
    runner, inst = _crashing(runner_config, monkeypatch)
    inst.task_limit_note = "A task limit refused 3 from an earlier process."
    _live(monkeypatch, False)
    await runner.poll_once()
    assert inst.status is InstanceStatus.CRASHED
    assert inst.task_limit_note is None


async def test_poll_drops_a_baseline_it_can_no_longer_read(runner_config, monkeypatch, roots):
    cgroup, _proc = roots
    runner, inst = _crashing(runner_config, monkeypatch)
    path = _group(cgroup, "svc", 13)
    counts = runner._poll_loop._task_counts
    counts[inst.instance_id] = task_limits.TaskEventCount(inst.bridge_pid, path, 0, None)
    _live(monkeypatch, True)
    await runner.poll_once()  # no /proc entry for this pid, so no fresh baseline
    assert inst.instance_id not in counts


async def test_poll_prunes_baselines_of_removed_rows(runner_config, monkeypatch, roots):
    cgroup, _proc = roots
    runner, _inst = _crashing(runner_config, monkeypatch)
    path = _group(cgroup, "svc", 1)
    counts = runner._poll_loop._task_counts
    counts["gone"] = task_limits.TaskEventCount(1, path, 0, None)
    _live(monkeypatch, True)
    await runner.poll_once()
    assert "gone" not in counts


async def test_observing_poll_takes_no_baseline(runner_config, monkeypatch, roots):
    cgroup, proc = roots
    runner, inst = _crashing(runner_config, monkeypatch)
    _group(cgroup, "svc", 1)
    _process(proc, inst.bridge_pid, "svc")
    _live(monkeypatch, True)
    await runner.poll_once(side_effects=False)
    assert runner._poll_loop._task_counts == {}


def test_crash_notification_carries_the_reason():
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.CRASHED)
    _title, plain = RecordFacade._notify_message("crash", inst)
    assert "likely" not in plain
    inst.task_limit_note = "A task limit refused 1 new process. This is the likely cause."
    _title, body = RecordFacade._notify_message("crash", inst)
    assert body.endswith(inst.task_limit_note)


def _systemctl(monkeypatch, stdout="", returncode=0, present=True, error=None):
    monkeypatch.setattr(
        ops.shutil, "which", lambda name: "/usr/bin/systemctl" if present else None
    )

    def _run(*_args, **_kwargs):
        if error is not None:
            raise error
        return subprocess.CompletedProcess([], returncode, stdout, "")

    monkeypatch.setattr(ops.subprocess, "run", _run)


def _props(cgroup="/svc", current="650", limit="76640", state="loaded"):
    return f"LoadState={state}\nControlGroup={cgroup}\nTasksCurrent={current}\nTasksMax={limit}\n"


def test_service_tasks_none_without_systemctl(monkeypatch):
    _systemctl(monkeypatch, present=False)
    assert ops._check_service_tasks() is None


def test_service_tasks_none_on_query_error(monkeypatch):
    _systemctl(monkeypatch, error=OSError("no"))
    assert ops._check_service_tasks() is None


def test_service_tasks_none_on_failed_query(monkeypatch):
    _systemctl(monkeypatch, returncode=1)
    assert ops._check_service_tasks() is None


def test_service_tasks_none_when_not_loaded(monkeypatch):
    _systemctl(monkeypatch, _props(state="not-found"))
    assert ops._check_service_tasks() is None


def test_service_tasks_ok_with_no_refusals(monkeypatch, roots):
    cgroup, _proc = roots
    _group(cgroup, "svc", 0)
    _systemctl(monkeypatch, _props())
    check = ops._check_service_tasks()
    assert check is not None and check.status == ops.OK
    assert "650 tasks, limit 76640" in check.detail
    assert "no refused" in check.detail


def test_service_tasks_warns_on_refusals(monkeypatch, roots):
    cgroup, _proc = roots
    _group(cgroup, "svc", 430)
    _systemctl(monkeypatch, _props())
    check = ops._check_service_tasks()
    assert check is not None and check.status == ops.WARN
    assert "refused 430" in check.detail and "can be old" in check.detail


def test_service_tasks_without_a_counter_makes_no_claim(monkeypatch, roots):
    _systemctl(monkeypatch, _props(cgroup=""))
    check = ops._check_service_tasks()
    assert check is not None and check.status == ops.OK
    assert "refused" not in check.detail


@pytest.mark.parametrize("limit", ["infinity", "18446744073709551615"])
def test_service_tasks_no_limit_and_unknown_count(monkeypatch, roots, limit):
    _systemctl(monkeypatch, _props(current="[not set]", limit=limit))
    check = ops._check_service_tasks()
    assert check is not None and "unknown tasks, limit no limit" in check.detail


def test_doctor_bridge_check_warns_on_slice_refusals(tmp_path, monkeypatch):
    from clauster.config import ClausterConfig

    support = bridge_scope.ScopeSupport(True, "ok", "/e", "/s", "/run/user/1")
    monkeypatch.setattr(bridge_scope, "probe_scope_support", lambda: support)
    monkeypatch.setattr(ops.claude_cli, "resolve_binary", lambda _b: "/opt/claude")
    monkeypatch.setattr(task_limits, "bridge_slice_refused", lambda: 3)
    config = ClausterConfig(
        projects_root=tmp_path,
        state_dir=tmp_path / "state",
        instance_defaults={"bridge_task_limit": 30000},
    )
    check = ops._check_bridge_task_limit(config)
    assert check is not None and check.status == ops.WARN and "refused 3" in check.detail


def test_doctor_output_includes_service_tasks(write_config, tmp_path, monkeypatch):
    check = ops.Check("service tasks", ops.OK, "x")
    monkeypatch.setattr(ops, "_check_service_tasks", lambda: check)
    path = tmp_path / "clauster.yml"
    (tmp_path / "projects").mkdir()
    path.write_text(
        f"schema_version: 1\nprojects_root: {(tmp_path / 'projects').as_posix()}\n"
        f"state_dir: {(tmp_path / 'state').as_posix()}\n",
        encoding="utf-8",
    )
    checks, _ok = ops.run_doctor(path)
    assert check in checks


async def test_adopted_process_does_not_inherit_the_old_crash_note(
    runner_config, monkeypatch, roots
):
    cgroup, proc = roots
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    project = next(iter(runner._discovered()))
    inst = RemoteControlInstance(project=project, label=project, status=InstanceStatus.RUNNING)
    inst.bridge_pid = 4242
    runner._instances[inst.instance_id] = inst
    iid = inst.instance_id
    _group(cgroup, "svc", 10)
    _process(proc, 4242, "svc")
    notes = []
    monkeypatch.setattr(
        runner._record,
        "_emit_lifecycle",
        lambda event, row: notes.append(row.task_limit_note) if event == "crash" else None,
    )
    monkeypatch.setattr("clauster.runner.procutil.reap_if_exited", lambda *a, **k: None)
    monkeypatch.setattr("clauster.poll_loop.inspector.list_working_sessions", lambda *a, **k: [])
    saved: dict = {}

    async def _refresh():
        runner._registry._persisted.clear()
        if saved:
            runner._registry._persisted[iid] = dict(saved)
        return True

    monkeypatch.setattr(runner._registry, "_refresh_persisted", _refresh)
    _live(monkeypatch, True)
    await runner.poll_once()
    _group(cgroup, "svc", 13)
    _live(monkeypatch, False)
    await runner.poll_once()
    assert inst.task_limit_note is not None

    # Another clauster process resumed the row as pid 5555, which stays alive.
    saved.update(project_name=project, bridge_pid=5555, bridge_proc_start=1.0)
    monkeypatch.setattr(
        "clauster.runner.procutil.is_live_bridge", lambda pid, *_a, **_k: pid == 5555
    )
    await runner.poll_once()
    assert runner._instances[iid] is inst and inst.bridge_pid == 5555
    assert inst.status is not InstanceStatus.CRASHED
    assert inst.task_limit_note is None
