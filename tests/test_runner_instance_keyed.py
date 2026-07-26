"""Instance-keyed rediscover / cross-process adoption (#1088, #1091, #1096).

The defect these pin: state that #777/#778 made instance-keyed was still being *read*
project-keyed. ``rediscover`` walked projects, so it could materialize at most ONE instance
per project, and picked which one by first-match rather than by any correlation with the
live process. A project running a standard bridge plus N interactive sessions therefore
showed a single, often stale, row to ``clauster status`` / ``clauster mcp`` — and
``clauster stop <id>`` failed for every id except whichever the dict happened to yield first.
"""

from __future__ import annotations

import asyncio

import pytest

from clauster.models import InstanceStatus, RemoteControlInstance
from clauster.runner import SessionRunner, _row_float, _row_int

pytestmark = pytest.mark.anyio


def _make_runner(runner_config) -> SessionRunner:
    config, claude_json = runner_config
    return SessionRunner(config, claude_json=claude_json)


def _stub_connect(monkeypatch, facts=None):
    """Stub connect-fact recovery for tests about identity rather than readiness.

    Adoption promotes to RUNNING only with readiness evidence (a live pointer / ready
    sidecar) — liveness is not usability. Tests that care about WHICH rows materialize
    supply that evidence here; the recovery itself is covered by its own tests below.
    """
    monkeypatch.setattr(
        SessionRunner,
        "_connect_facts_for",
        lambda self, proj, mode, pid, start: dict(
            facts or {"url": "https://claude.ai/code?environment=env_STUB"}
        ),
    )


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
    _stub_connect(monkeypatch)
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
    _stub_connect(monkeypatch)
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


async def test_stopped_row_does_not_hide_a_live_external_bridge(runner_config, monkeypatch):
    # MF-1 regression. The row pass inserts a STOPPED card for a dead row; that card must NOT
    # suppress the pointer walk, which is the only way a bridge someone else started is found
    # at startup. Guarding the walk on `get_instance_for_project` did exactly that, because it
    # matches in ANY status (#778) — so the projects that most needed the walk were the ones
    # that skipped it. Differential: on `main` the pointer is consulted once and the instance
    # comes back RUNNING; unfixed, it is consulted zero times and the card stays STOPPED.
    runner = _make_runner(runner_config)
    _stub_connect(monkeypatch)
    runner.persistence.state_store().save(
        {"iid-dead": _row("alpha", pid=5002, resume_mode="standard")}
    )
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)

    consulted: list = []

    class _ExternalPtr:
        pid = 9001
        proc_start = "1000"
        environment_id = "env_EXTERNAL"
        session_id = "session_EXTERNAL"

    def _pointer_for_project(path):
        consulted.append(path)
        return _ExternalPtr()

    monkeypatch.setattr("clauster.runner.pointers.pointer_for_project", _pointer_for_project)
    monkeypatch.setattr("clauster.runner.pointers.is_live", lambda ptr: True)

    await runner.rediscover(persist=False)

    assert consulted, "pointer walk skipped: the STOPPED card suppressed external discovery"
    alpha = [i for i in runner.list_instances() if i.project == "alpha"]
    assert len(alpha) == 1, f"the walk must reuse the row's id, not add a card: {alpha}"
    assert alpha[0].instance_id == "iid-dead"
    assert alpha[0].status is InstanceStatus.RUNNING
    assert alpha[0].bridge_pid == 9001
    assert alpha[0].environment_id == "env_EXTERNAL"


async def test_stopped_row_does_not_hide_a_live_detached_keeper(runner_config, monkeypatch):
    # The pty half of the same defect: with no Anthropic pointer, the walk's keeper-sidecar
    # leg is what re-manages a detached keeper that outlived the restart. Blocked by the same
    # guard, it never ran, so a LIVE keeper leaked behind a STOPPED card — and the new prune
    # then deleted that card, leaving the operator with no handle on a running bridge.
    runner = _make_runner(runner_config)
    _stub_connect(monkeypatch)
    runner.persistence.state_store().save({"iid-dead-pty": _row("alpha", pid=5003)})
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    monkeypatch.setattr("clauster.runner.pointers.pointer_for_project", lambda path: None)

    sidecar_calls: list = []
    monkeypatch.setattr(
        SessionRunner,
        "_reattach_pty_from_sidecar",
        lambda self, name, saved, iid: sidecar_calls.append(name),
    )

    await runner.rediscover(persist=False)

    # Other fixture projects are walked too; `alpha` is the one with the STOPPED card, and
    # pre-fix it was the only one MISSING from this list.
    assert "alpha" in sidecar_calls, "the keeper-sidecar leg never ran: a live keeper leaks"


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
    _stub_connect(monkeypatch)
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
    _stub_connect(monkeypatch)
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    # No sidecar correlates a keeper to THIS bridge, so none may be adopted: the row's
    # keeper pid is not trusted on its own, because `stop()` force-kills that pid's tree.
    monkeypatch.setattr(SessionRunner, "_recover_keeper_pid", lambda self, n, p, s: None)
    await runner.rediscover(persist=False)

    inst = runner.get_instance("iid-a")
    assert inst is not None
    assert inst.status is InstanceStatus.RUNNING, "the bridge itself is still alive"
    assert inst.keeper_pid is None, "an uncorrelated keeper pid must not be adopted"


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


# ----- part 3: cross-process adoption (#1091) --------------------------------


async def test_poll_adopts_a_bridge_another_process_started(runner_config, monkeypatch):
    # THE #1091 regression test. `start_poll_loop` called rediscover ONCE and poll_once then
    # iterated only `self._instances`, never re-reading the shared store — so a bridge from
    # `clauster start` or the MCP server was never adopted, and the agents --json cross-check
    # labelled its live process EXTERNAL/unmanaged with no controls in the dashboard.
    runner = _make_runner(runner_config)
    _stub_connect(monkeypatch)
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda *a, **k: True)
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    assert runner.list_instances() == []

    # Another process writes a row to the shared store while we are already running.
    runner.persistence.state_store().save({"iid-cli": _row("alpha", pid=3001)})
    await runner.poll_once()

    adopted = runner.get_instance("iid-cli")
    assert adopted is not None, "a row created by another process must be adopted"
    assert adopted.status is InstanceStatus.RUNNING
    assert adopted.bridge_pid == 3001


async def test_poll_adoption_cannot_undo_a_forget(runner_config, monkeypatch):
    # The hazard of refreshing the merge base every tick: adoption must never resurrect a
    # row another process deliberately removed. `_refresh_persisted` replaces the base with
    # the CURRENT store, so a forgotten row is simply absent and cannot come back.
    runner = _make_runner(runner_config)
    store = runner.persistence.state_store()
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda *a, **k: True)
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])

    store.save({"iid-a": _row("alpha", pid=3101)})
    await runner.poll_once()
    assert runner.get_instance("iid-a") is not None

    store.save({})  # another process forgets it
    del runner._instances["iid-a"]  # ...and it leaves our registry too
    await runner.poll_once()

    assert runner.get_instance("iid-a") is None, "a forgotten row must not be re-adopted"


async def test_poll_does_not_resurrect_dead_rows_as_stopped_cards(runner_config, monkeypatch):
    # Poll-time adoption is LIVE-only. Resurrecting dead rows every tick would fight the
    # phantom-prune below, which deletes exactly such cards.
    runner = _make_runner(runner_config)
    runner.persistence.state_store().save({"iid-dead": _row("alpha", pid=3201)})
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    await runner.poll_once()
    assert runner.get_instance("iid-dead") is None


# ----- part 4: the phantom-prune (#1096) -------------------------------------


def _external_session(cwd, pid=999):
    from clauster.models import WorkingSession

    return WorkingSession(pid=pid, cwd=cwd, kind="interactive", started_at=999, local_uuid="u")


async def test_prune_does_not_wipe_unrelated_stopped_siblings(runner_config, monkeypatch):
    # #1096 over-prune. Two DISTINCT resumable interactive sessions plus one unmanaged
    # bridge at the project root previously deleted BOTH cards — one external process
    # cannot be two bridges, and the operator lost the Resume affordance for both.
    config = runner_config[0]
    runner = _make_runner(runner_config)
    for iid, label in (("iid-a", "session-A"), ("iid-b", "session-B")):
        runner._instances[iid] = runner._stopped_from_row(
            iid, {"project_name": "alpha", "label": label, "resume_mode": "pty"}
        )
    monkeypatch.setattr("clauster.runner.procutil.is_bridge_process", lambda pid: True)
    monkeypatch.setattr(
        "clauster.inspector.list_working_sessions",
        lambda *a, **k: [_external_session(config.projects_root / "alpha")],
    )
    await runner.poll_once()

    survivors = sorted(i.label for i in runner.list_instances() if i.project == "alpha")
    assert survivors == ["session-A", "session-B"], (
        "one external bridge cannot identify which stopped card is the phantom, so neither "
        "may be deleted"
    )


async def test_prune_still_removes_a_lone_phantom(runner_config, monkeypatch):
    # The behaviour the prune exists for must survive the fix: a single stopped card
    # shadowing a live unmanaged bridge at the same cwd is a phantom and still goes.
    config = runner_config[0]
    runner = _make_runner(runner_config)
    runner._instances["iid-a"] = runner._stopped_from_row(
        "iid-a", {"project_name": "alpha", "label": "alpha", "resume_mode": "pty"}
    )
    monkeypatch.setattr("clauster.runner.procutil.is_bridge_process", lambda pid: True)
    monkeypatch.setattr(
        "clauster.inspector.list_working_sessions",
        lambda *a, **k: [_external_session(config.projects_root / "alpha")],
    )
    await runner.poll_once()

    assert runner.get_instance("iid-a") is None, "a lone phantom must still be pruned"
    assert "alpha" in runner.external_sessions_by_project()


async def test_prune_removes_a_phantom_even_when_a_sibling_is_live(runner_config, monkeypatch):
    # #1096 UNDER-prune — the direction the old code got wrong in the opposite way. A live
    # sibling put the whole project into `live_projects`, so a genuine phantom was never
    # pruned and kept offering a misleading Stopped/Resume card right next to a live
    # unmanaged bridge. The prune must decide per INSTANCE, not per project.
    config = runner_config[0]
    runner = _make_runner(runner_config)
    live = RemoteControlInstance(
        instance_id="iid-live",
        project="alpha",
        label="live-pty",
        resume_mode="pty",
        status=InstanceStatus.RUNNING,
        bridge_pid=4242,
    )
    runner._instances["iid-live"] = live
    runner._instances["iid-phantom"] = runner._stopped_from_row(
        "iid-phantom", {"project_name": "alpha", "label": "phantom", "resume_mode": "pty"}
    )
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    # The external session's pid is NOT a descendant of the live bridge, so reconcile
    # correctly classes it EXTERNAL rather than folding it into the live instance.
    monkeypatch.setattr("clauster.runner.procutil.owned_pids", lambda roots: set(roots))
    monkeypatch.setattr("clauster.runner.procutil.is_bridge_process", lambda pid: True)
    monkeypatch.setattr(
        "clauster.inspector.list_working_sessions",
        lambda *a, **k: [_external_session(config.projects_root / "alpha", pid=999)],
    )
    await runner.poll_once()

    assert runner.get_instance("iid-live") is not None, "the live sibling must survive"
    assert runner.get_instance("iid-phantom") is None, (
        "a live sibling must not shield a phantom from the prune"
    )


async def test_poll_adoption_skips_when_the_store_read_fails(runner_config, monkeypatch):
    # Fail-closed. `_refresh_persisted` keeps the OLD base on a read error rather than
    # replacing it with nothing, so adopting off that stale base could take over rows the
    # store no longer has. On a failed refresh, adopt nothing this tick and try again next.
    runner = _make_runner(runner_config)
    runner.persistence.state_store().save({"iid-a": _row("alpha", pid=3301)})
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])

    async def _failed_refresh() -> bool:
        return False

    monkeypatch.setattr(runner, "_refresh_persisted", _failed_refresh)
    await runner.poll_once()

    assert runner.get_instance("iid-a") is None, "a failed store read must not adopt anything"


# ----- regressions found by dogfooding the fix (not by the tests above) ------


async def test_reattached_standard_bridge_keeps_its_connect_link(runner_config, monkeypatch):
    # Found on a live instance: the dashboard sat on "Preparing connect link…" forever for
    # every reattached/adopted bridge. The row carries identity and liveness but NOT the
    # connect facts — those are written by the bridge into the Anthropic pointer — so a
    # reattach that reads only the row produces a managed bridge nobody can connect to,
    # which defeats the point of having it.
    from clauster import pointers

    runner = _make_runner(runner_config)
    runner.persistence.state_store().save(
        {"iid-a": _row("alpha", pid=7701, proc_start=None, resume_mode="standard")}
    )
    ptr = pointers.BridgePointer(
        pid=7701,
        proc_start="123",
        source="test",
        environment_id="env_ABC",
        session_id="session_XYZ",
    )
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    monkeypatch.setattr("clauster.runner.pointers.pointer_for_project", lambda *a, **k: ptr)
    await runner.rediscover(persist=False)

    inst = runner.get_instance("iid-a")
    assert inst is not None
    assert inst.environment_id == "env_ABC"
    assert inst.url == "https://claude.ai/code?environment=env_ABC"
    assert inst.starter_session_id == "session_XYZ"


async def test_connect_facts_are_not_borrowed_from_another_bridge(runner_config, monkeypatch):
    # The pointer lives at the PROJECT path, so it may have been written by a different
    # bridge than the row being reattached. Matching on pid stops one instance inheriting
    # another's environment — which would deep-link the operator into the wrong session.
    from clauster import pointers

    runner = _make_runner(runner_config)
    runner.persistence.state_store().save(
        {"iid-a": _row("alpha", pid=7801, proc_start=None, resume_mode="standard")}
    )
    other = pointers.BridgePointer(
        pid=9999,
        proc_start="123",
        source="test",
        environment_id="env_SOMEONE_ELSE",
        session_id="s",
    )
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    monkeypatch.setattr("clauster.runner.pointers.pointer_for_project", lambda *a, **k: other)
    await runner.rediscover(persist=False)

    inst = runner.get_instance("iid-a")
    assert inst is not None
    assert inst.environment_id is None, "a pointer for a different pid must not be adopted"
    assert inst.url is None


async def test_a_stop_by_another_process_is_not_reported_as_a_crash(runner_config, monkeypatch):
    # Found on a live instance: stop a CLI-started bridge from the CLI and the dashboard
    # showed "Crashed — the bridge exited unexpectedly". `stop()` records the intent in the
    # ROW, but the server's adopted copy still said intentional_stop=False, so when the
    # process exited `_reconcile_status` had no intent to go on. Exactly the cross-process
    # divergence this change exists to remove.
    runner = _make_runner(runner_config)
    _stub_connect(monkeypatch)
    store = runner.persistence.state_store()
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])

    store.save({"iid-a": _row("alpha", pid=7901)})
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda *a, **k: True)
    await runner.poll_once()
    adopted = runner.get_instance("iid-a")
    assert adopted is not None and adopted.status is InstanceStatus.RUNNING

    # Another process stops it: the row records the intent, then the process exits.
    store.save({"iid-a": _row("alpha", pid=7901, intentional_stop=True)})
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    await runner.poll_once()

    inst = runner.get_instance("iid-a")
    assert inst is not None
    assert inst.status is InstanceStatus.STOPPED, (
        "a deliberate stop from another process must not be reported as a crash"
    )


async def test_reattached_pty_bridge_keeps_its_connect_link(runner_config, monkeypatch):
    # The pty half of the same regression: a flag-form bridge writes no Anthropic pointer,
    # so its connect URL lives in the keeper sidecar. Matched on bridge pid so one interactive
    # session cannot inherit another's link — several share one project's log directory.
    import json

    runner = _make_runner(runner_config)
    runner.persistence.state_store().save({"iid-a": _row("alpha", pid=8801)})
    runner._log_dir.mkdir(parents=True, exist_ok=True)
    (runner._log_dir / "alpha-333.keeper.json").write_text(
        json.dumps(
            {"bridge_pid": 9999, "state": "ready", "connect_url": "https://claude.ai/code/OTHER"}
        )
    )
    (runner._log_dir / "alpha-222.keeper.json").write_text(
        json.dumps(
            {
                "bridge_pid": 8801,
                "state": "ready",
                "connect_url": "https://claude.ai/code/MINE",
                "session_id": "session_MINE",
            }
        )
    )
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda *a, **k: True)
    await runner.rediscover(persist=False)

    inst = runner.get_instance("iid-a")
    assert inst is not None
    assert inst.url == "https://claude.ai/code/MINE", "must match on pid, not just newest"
    assert inst.starter_session_id == "session_MINE"


async def test_reattached_pty_bridge_with_no_sidecar_has_no_link(runner_config, monkeypatch):
    # No sidecar match -> no facts. Showing the honest "preparing connect link" state beats
    # inventing a link that deep-links the operator somewhere wrong.
    runner = _make_runner(runner_config)
    runner.persistence.state_store().save({"iid-a": _row("alpha", pid=8901)})
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda *a, **k: True)
    await runner.rediscover(persist=False)

    inst = runner.get_instance("iid-a")
    assert inst is not None and inst.url is None


async def test_connect_facts_reject_a_pointer_whose_start_time_differs(runner_config, monkeypatch):
    # Same pid, different start time: the pid was recycled, so the pointer belongs to some
    # other process and its environment must not be handed to this row.
    from clauster import pointers

    runner = _make_runner(runner_config)
    runner.persistence.state_store().save(
        {"iid-a": _row("alpha", pid=8701, proc_start=999.0, resume_mode="standard")}
    )
    ptr = pointers.BridgePointer(
        pid=8701,
        proc_start="123",  # -> a different epoch than 999.0
        source="test",
        environment_id="env_STALE",
        session_id="s",
    )
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    monkeypatch.setattr("clauster.runner.pointers.pointer_for_project", lambda *a, **k: ptr)
    monkeypatch.setattr("clauster.runner.procutil._expected_epoch", lambda _s: 111.0)
    await runner.rediscover(persist=False)

    inst = runner.get_instance("iid-a")
    assert inst is not None
    assert inst.environment_id is None, "a recycled pid must not inherit the old environment"


async def test_pty_sidecar_without_a_session_id_still_yields_the_url(runner_config, monkeypatch):
    # The sidecar is raw JSON written by the keeper, so unlike the pointer its fields really
    # can be absent — a bridge that is up but has not yet minted a session has a connect URL
    # and no session id. Take what is there rather than discarding both.
    import json

    runner = _make_runner(runner_config)
    runner.persistence.state_store().save({"iid-a": _row("alpha", pid=8601)})
    runner._log_dir.mkdir(parents=True, exist_ok=True)
    (runner._log_dir / "alpha-444.keeper.json").write_text(
        json.dumps(
            {
                "bridge_pid": 8601,
                "state": "ready",
                "connect_url": "https://claude.ai/code/NOSESSION",
            }
        )
    )
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda *a, **k: True)
    await runner.rediscover(persist=False)

    inst = runner.get_instance("iid-a")
    assert inst is not None
    assert inst.url == "https://claude.ai/code/NOSESSION"
    assert inst.starter_session_id is None


async def test_pty_sidecar_without_a_url_still_yields_the_session(runner_config, monkeypatch):
    # The mirror case: a keeper that recorded its session before the connect URL resolved.
    import json

    runner = _make_runner(runner_config)
    runner.persistence.state_store().save({"iid-a": _row("alpha", pid=8501)})
    runner._log_dir.mkdir(parents=True, exist_ok=True)
    (runner._log_dir / "alpha-555.keeper.json").write_text(
        json.dumps({"bridge_pid": 8501, "state": "ready", "session_id": "session_ONLY"})
    )
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda *a, **k: True)
    await runner.rediscover(persist=False)

    inst = runner.get_instance("iid-a")
    assert inst is not None
    assert inst.url is None
    assert inst.starter_session_id == "session_ONLY"


async def test_prune_ignores_a_hand_run_claude_that_is_not_a_bridge(runner_config, monkeypatch):
    # #1096 / #820. An operator running `claude` in a terminal at the project root is
    # EXTERNAL by design, but it is NOT an unmanaged bridge — so it is no evidence that a
    # stopped card is a phantom. Deleting a resumable session because someone opened a
    # terminal there would be a silent data-visibility loss.
    config = runner_config[0]
    runner = _make_runner(runner_config)
    runner._instances["iid-a"] = runner._stopped_from_row(
        "iid-a", {"project_name": "alpha", "label": "alpha", "resume_mode": "pty"}
    )
    monkeypatch.setattr("clauster.runner.procutil.is_bridge_process", lambda pid: False)
    monkeypatch.setattr(
        "clauster.inspector.list_working_sessions",
        lambda *a, **k: [_external_session(config.projects_root / "alpha")],
    )
    await runner.poll_once()

    assert runner.get_instance("iid-a") is not None, (
        "a non-bridge external session must not trigger the phantom prune"
    )


async def test_adoption_does_not_promote_an_unready_bridge_to_running(runner_config, monkeypatch):
    # Liveness is not usability. A bridge whose process is up but which has not registered
    # an environment (no live pointer / no ready sidecar) is STARTING — promoting on a bare
    # pid would report uncontrollable bridges as RUNNING, which the reconcile invariant
    # explicitly forbids.
    runner = _make_runner(runner_config)
    runner.persistence.state_store().save({"iid-a": _row("alpha", pid=6601)})
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    monkeypatch.setattr(SessionRunner, "_recover_keeper_pid", lambda self, n, p, s: None)
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    await runner.poll_once()  # no pointer, no sidecar -> no readiness evidence

    inst = runner.get_instance("iid-a")
    assert inst is not None
    assert inst.status is InstanceStatus.STARTING, "a live-but-unready bridge is STARTING"


async def test_intent_sync_ignores_a_row_from_a_different_process_generation(
    runner_config, monkeypatch
):
    # The `intentional_stop` sync must only trust a row describing the SAME incarnation.
    # A resume reuses the instance id with a FRESH pid while the store still holds the
    # previous stop's intent; syncing that would mark the new bridge stopped-on-purpose and
    # silently suppress crash reporting for it.
    runner = _make_runner(runner_config)
    store = runner.persistence.state_store()
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)

    store.save({"iid-a": _row("alpha", pid=5501, intentional_stop=True)})  # the OLD pid
    runner._instances["iid-a"] = RemoteControlInstance(
        instance_id="iid-a",
        project="alpha",
        label="alpha",
        status=InstanceStatus.STARTING,
        intentional_stop=False,
        bridge_pid=5502,  # resumed: a different process generation
        bridge_proc_start=222.0,
    )
    await runner.poll_once()

    inst = runner.get_instance("iid-a")
    assert inst is not None
    assert inst.intentional_stop is False, (
        "a row describing the previous incarnation must not mark the resumed bridge stopped"
    )


async def test_pids_resync_when_another_process_resumed_the_bridge(runner_config, monkeypatch):
    # Cross-process pid clobber. If another process resumes an instance we track, our copy
    # holds the OLD pid — and our next `_persist` would overlay it back over the row, making
    # the live bridge read dead to every reader. That is the #1088 symptom re-introduced
    # through the very columns added to fix it.
    runner = _make_runner(runner_config)
    store = runner.persistence.state_store()
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    runner._instances["iid-a"] = RemoteControlInstance(
        instance_id="iid-a",
        project="alpha",
        label="alpha",
        status=InstanceStatus.STOPPED,
        bridge_pid=4401,  # ours: dead
        bridge_proc_start=100.0,
    )
    store.save({"iid-a": _row("alpha", pid=4402, proc_start=200.0)})  # theirs: live
    monkeypatch.setattr(
        "clauster.runner.procutil.is_live_bridge", lambda pid, _s=None: pid == 4402
    )
    await runner.poll_once()

    inst = runner.get_instance("iid-a")
    assert inst is not None
    assert inst.bridge_pid == 4402, "must take the live pids another process recorded"
    assert inst.bridge_proc_start == 200.0
    assert inst.status is InstanceStatus.RUNNING


async def test_adoption_failure_does_not_abort_the_poll(runner_config, monkeypatch):
    # Adoption runs first in the tick, so an exception there would take crash detection,
    # the cross-check and the prune down with it — a permanently degraded poll loop for as
    # long as the offending row exists. Best-effort, like the `agents --json` probe.
    runner = _make_runner(runner_config)
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])

    async def _boom() -> None:
        raise RuntimeError("malformed row")

    monkeypatch.setattr(runner, "_adopt_rows_from_store", _boom)
    await runner.poll_once()  # must not raise


async def test_pty_sidecar_must_be_ready_and_pid_correlated(runner_config, monkeypatch):
    # A sidecar that is mid-startup, or whose proc-start says it belongs to a recycled pid,
    # must not lend its connect URL — several interactive sessions share one log directory.
    import json

    runner = _make_runner(runner_config)
    runner.persistence.state_store().save({"iid-a": _row("alpha", pid=8401, proc_start=500.0)})
    runner._log_dir.mkdir(parents=True, exist_ok=True)
    (runner._log_dir / "alpha-900.keeper.json").write_text(
        json.dumps({"bridge_pid": 8401, "state": "starting", "connect_url": "u-notready"})
    )
    (runner._log_dir / "alpha-800.keeper.json").write_text(
        json.dumps(
            {
                "bridge_pid": 8401,
                "state": "ready",
                "bridge_proc_start": 9999.0,  # a different incarnation of this pid
                "connect_url": "u-stale",
            }
        )
    )
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    monkeypatch.setattr(SessionRunner, "_recover_keeper_pid", lambda self, n, p, s: None)
    await runner.rediscover(persist=False)

    inst = runner.get_instance("iid-a")
    assert inst is not None
    assert inst.url is None, "neither an unready nor a mismatched sidecar may be used"


def test_is_bridge_process_fails_closed(monkeypatch):
    # Any psutil error means "not a bridge": the prune deletes cards on the strength of
    # this answer, so an unreadable process must never read as one.
    import psutil

    from clauster import procutil as pu

    def _raise(_pid):
        raise psutil.AccessDenied(_pid)

    monkeypatch.setattr(psutil, "Process", _raise)
    assert pu.is_bridge_process(4242) is False


async def test_adoption_does_not_overwrite_an_instance_a_lock_holder_created(
    runner_config, monkeypatch
):
    # TOCTOU. The poll is lock-free and its "already tracked?" check is separated from the
    # insert by several awaits. A lock-holding adopt()/spawn() landing in that window has
    # already handed its object to an HTTP caller — overwriting it would leave the response
    # describing an object the registry no longer holds.
    runner = _make_runner(runner_config)
    runner.persistence.state_store().save({"iid-a": _row("alpha", pid=3401)})
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    monkeypatch.setattr(SessionRunner, "_recover_keeper_pid", lambda self, n, p, s: None)

    winner = RemoteControlInstance(
        instance_id="iid-a", project="alpha", label="from-adopt", status=InstanceStatus.RUNNING
    )

    def _land_concurrently(self, proj, mode, pid, start):
        # Simulates the lock-holder committing during one of the awaits above the insert.
        self._instances["iid-a"] = winner
        return {"url": "https://claude.ai/code?environment=env_STUB"}

    monkeypatch.setattr(SessionRunner, "_connect_facts_for", _land_concurrently)
    await runner.poll_once()

    assert runner.get_instance("iid-a") is winner, (
        "the lock-holder's instance must survive the lock-free poll's insert"
    )


async def test_adoption_cancellation_propagates(runner_config, monkeypatch):
    # The best-effort guard must not swallow CancelledError, or shutdown would hang while
    # the poll loop kept going.
    runner = _make_runner(runner_config)
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])

    async def _cancelled() -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(runner, "_adopt_rows_from_store", _cancelled)
    with pytest.raises(asyncio.CancelledError):
        await runner.poll_once()
