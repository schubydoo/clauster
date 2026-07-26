"""Instance-keyed rediscover / cross-process adoption (#1088, #1091, #1096).

The defect these pin: state that #777/#778 made instance-keyed was still being *read*
project-keyed. ``rediscover`` walked projects, so it could materialize at most ONE instance
per project, and picked which one by first-match rather than by any correlation with the
live process. A project running a standard bridge plus N interactive sessions therefore
showed a single, often stale, row to ``clauster status`` / ``clauster mcp`` — and
``clauster stop <id>`` failed for every id except whichever the dict happened to yield first.
"""

from __future__ import annotations

import pytest

from clauster.models import InstanceStatus
from clauster.runner import SessionRunner, _row_float, _row_int

pytestmark = pytest.mark.anyio


def _make_runner(runner_config) -> SessionRunner:
    config, claude_json = runner_config
    return SessionRunner(config, claude_json=claude_json)


def _row(project: str, *, pid: int | None, proc_start: float | None = 111.0, **extra) -> dict:
    row = {"project_name": project, "label": project, "resume_mode": "pty", **extra}
    if pid is not None:
        row["bridge_pid"] = pid
        row["bridge_proc_start"] = proc_start
    return row


async def test_rediscover_materializes_every_row_for_one_project(runner_config, monkeypatch):
    # THE #1088 regression test. Six rows on one project — the exact shape reported: one
    # standard bridge plus five interactive sessions. Before the fix rediscover returned
    # exactly ONE instance per project, so five of these vanished from every fresh process.
    runner = _make_runner(runner_config)
    rows = {
        "iid-standard": _row("alpha", pid=4001, resume_mode="standard"),
        "iid-pty-1": _row("alpha", pid=4002),
        "iid-pty-2": _row("alpha", pid=4003),
        "iid-pty-3": _row("alpha", pid=4004),
        "iid-pty-4": _row("alpha", pid=4005),
        "iid-pty-5": _row("alpha", pid=4006),
    }
    runner.persistence.state_store().save(rows)
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda *a, **k: True)

    await runner.rediscover(persist=False)

    got = {i.instance_id for i in runner.list_instances() if i.project == "alpha"}
    assert got == set(rows), f"expected all six rows, got {sorted(got)}"
    assert all(i.status is InstanceStatus.RUNNING for i in runner.list_instances())
    # Each keeps the id it was spawned with — that is what makes `clauster stop <id>` work.
    for iid in rows:
        assert runner.resolve_bridge_id(iid) == iid


async def test_rediscover_judges_each_row_on_its_own_liveness(runner_config, monkeypatch):
    # Mixed liveness on ONE project: the live row must come back RUNNING and the dead one
    # STOPPED-but-resumable. The old project-keyed walk collapsed these into a single
    # verdict, which is how live sessions ended up attributed to an already-stopped bridge.
    runner = _make_runner(runner_config)
    runner.persistence.state_store().save(
        {
            "iid-live": _row("alpha", pid=5001),
            "iid-dead": _row("alpha", pid=5002, intentional_stop=True),
        }
    )
    monkeypatch.setattr(
        "clauster.runner.procutil.is_live_bridge", lambda pid, _s=None: pid == 5001
    )
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda *a, **k: True)

    await runner.rediscover(persist=False)

    by_id = {i.instance_id: i for i in runner.list_instances()}
    assert by_id["iid-live"].status is InstanceStatus.RUNNING
    assert by_id["iid-live"].bridge_pid == 5001
    assert by_id["iid-dead"].status is InstanceStatus.STOPPED
    # A dead card must not carry the stale pid: that pid can be reused by an unrelated
    # process, and a later liveness check would then read the card as alive again.
    assert by_id["iid-dead"].bridge_pid is None
    assert by_id["iid-dead"].bridge_proc_start is None


async def test_rediscover_rejects_a_recycled_pid(runner_config, monkeypatch):
    # PID-reuse defence. The pid is alive but belongs to something else, which the
    # proc-start half of the pair detects. Persisting a bare pid would have let an unrelated
    # process be adopted as somebody's bridge.
    runner = _make_runner(runner_config)
    runner.persistence.state_store().save({"iid-a": _row("alpha", pid=6001, proc_start=100.0)})
    seen: list[tuple] = []

    def _fake_is_live(pid, proc_start=None):
        seen.append((pid, proc_start))
        return False  # same pid, different start time -> not our bridge

    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", _fake_is_live)
    await runner.rediscover(persist=False)

    assert seen == [(6001, 100.0)], "liveness must be checked as the (pid, proc_start) PAIR"
    inst = runner.get_instance("iid-a")
    assert inst is not None and inst.status is InstanceStatus.STOPPED


async def test_rediscover_falls_back_for_rows_written_before_the_pid_columns(
    runner_config, monkeypatch
):
    # Migration safety. A row from an older build carries no pids. Treating "no pid" as
    # "not live" would make the first boot after upgrade declare every surviving bridge
    # dead — a worse regression than the bug. Such a row must fall through to the legacy
    # pointer/sidecar walk instead, which here finds nothing and yields a STOPPED card.
    runner = _make_runner(runner_config)
    runner.persistence.state_store().save({"iid-legacy": _row("alpha", pid=None)})
    called: list[str] = []

    def _fake_is_live(*a, **k):
        called.append("is_live_bridge")
        return True

    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", _fake_is_live)
    await runner.rediscover(persist=False)

    assert not called, "a row with no persisted pid must not be pid-checked"
    inst = runner.get_instance("iid-legacy")
    assert inst is not None, "the legacy row must still surface, not vanish"
    assert inst.status is InstanceStatus.STOPPED


def test_row_coercion_helpers_reject_non_numbers():
    # Unit-level, because the DB cannot produce these: `instances.bridge_pid` is an INTEGER
    # column, so SQLite coerces `True` to 1 on the way back out. These guards cover the
    # payloads that DON'T come through the column — a row injected into `_persisted` by
    # another code path, or a future non-DB store — where `True` would otherwise resolve to
    # PID 1 and have liveness checked against init.
    assert _row_int(4242) == 4242
    assert _row_int(True) is None, "bool is a subclass of int; PID 1 is init"
    assert _row_int("4242") is None
    assert _row_int(None) is None
    assert _row_float(1.5) == 1.5
    assert _row_float(7) == 7.0, "an int proc-start is a valid float"
    assert _row_float(True) is None
    assert _row_float("nope") is None
    assert _row_float(None) is None


async def test_rediscover_survives_a_pid_with_no_proc_start(runner_config, monkeypatch):
    # A row can carry a pid but no proc-start (an interrupted write, or a pointer whose
    # procStart was unparseable). That must degrade to cmdline-only liveness — the same
    # fallback the pointer walk uses — not raise out of startup.
    runner = _make_runner(runner_config)
    runner.persistence.state_store().save(
        {"iid-a": {"project_name": "alpha", "label": "alpha", "bridge_pid": 9001}}
    )
    seen: list[tuple] = []

    def _fake_is_live(pid, proc_start=None):
        seen.append((pid, proc_start))
        return True

    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", _fake_is_live)
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda *a, **k: True)
    await runner.rediscover(persist=False)

    assert seen == [(9001, None)]
    inst = runner.get_instance("iid-a")
    assert inst is not None and inst.status is InstanceStatus.RUNNING


async def test_rediscover_leaves_an_already_tracked_instance_alone(runner_config, monkeypatch):
    # A row this process already owns (it spawned it) must not be rebuilt from the store.
    # Rebuilding would clobber the live object's runtime-only fields — the Popen handle's
    # peers, the connect URL, the log paths — with whatever the row happens to carry.
    runner = _make_runner(runner_config)
    runner.persistence.state_store().save({"iid-a": _row("alpha", pid=1234)})
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda *a, **k: True)
    await runner.rediscover(persist=False)

    live = runner.get_instance("iid-a")
    assert live is not None
    live.url = "https://claude.ai/code?environment=env_marker"  # a runtime-only field
    await runner.rediscover(persist=False)  # second pass, same row

    again = runner.get_instance("iid-a")
    assert again is not None
    assert again is live, "the tracked instance object must be preserved, not replaced"
    assert again.url == "https://claude.ai/code?environment=env_marker"


async def test_rediscover_drops_a_keeper_pid_that_is_no_longer_a_keeper(
    runner_config, monkeypatch
):
    # PID-reuse defence on the keeper half. If the recorded keeper pid now belongs to some
    # unrelated process, carrying it forward would let stop()/poll_once signal or reap a
    # stranger's process. Verified by cmdline, matching the sidecar reattach.
    runner = _make_runner(runner_config)
    runner.persistence.state_store().save({"iid-a": _row("alpha", pid=2001, keeper_pid=2002)})
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda pid: False)
    await runner.rediscover(persist=False)

    inst = runner.get_instance("iid-a")
    assert inst is not None
    assert inst.status is InstanceStatus.RUNNING, "the bridge itself is still alive"
    assert inst.keeper_pid is None, "a pid that is no longer a keeper must be dropped"


async def test_persist_round_trips_the_liveness_triple(runner_config, monkeypatch):
    # The write half: without these persisted there is nothing for a fresh process to read,
    # which is the root cause of both #1088 and #1091.
    runner = _make_runner(runner_config)
    runner.persistence.state_store().save({"iid-a": _row("alpha", pid=7001, proc_start=222.5)})
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda *a, **k: True)
    await runner.rediscover(persist=True)

    saved = runner.persistence.state_store().load()["iid-a"]
    assert saved["bridge_pid"] == 7001
    assert saved["bridge_proc_start"] == 222.5


async def test_stopped_rows_do_not_leak_pids_into_the_store(runner_config, monkeypatch):
    # A STOPPED card writes its pids back as absent, so the next process cannot mistake a
    # recycled pid for this bridge coming back to life.
    runner = _make_runner(runner_config)
    runner.persistence.state_store().save({"iid-a": _row("alpha", pid=8001)})
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    await runner.rediscover(persist=True)

    saved = runner.persistence.state_store().load()["iid-a"]
    assert saved.get("bridge_pid") is None
    assert saved.get("bridge_proc_start") is None
