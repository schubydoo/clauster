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
import json
import logging

import pytest

from clauster.models import InstanceStatus, RemoteControlInstance
from clauster.runner import InstanceStillLive, SessionRunner, _row_float, _row_int

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
    alpha = {i.instance_id: i for i in runner.list_instances() if i.project == "alpha"}
    # The live external bridge is materialized — the whole point of MF-1.
    running = [i for i in alpha.values() if i.status is InstanceStatus.RUNNING]
    assert len(running) == 1, f"the external bridge was not discovered: {list(alpha)}"
    assert running[0].bridge_pid == 9001
    assert running[0].environment_id == "env_EXTERNAL"
    # ...under an id of its OWN. An earlier revision had the walk adopt the dead row's id;
    # that is unsafe once a project has several rows (SF-4), because nothing proves the
    # external bridge is that row's session resumed rather than an unrelated one, and
    # guessing wrong overwrites a resumable record irrecoverably. Keeping them apart costs
    # a second card in the ambiguous case and loses nothing.
    assert running[0].instance_id != "iid-dead"
    assert alpha["iid-dead"].status is InstanceStatus.STOPPED, "the dead row lost its card"


async def test_walk_does_not_consume_another_cards_instance_id(runner_config, monkeypatch):
    # SF-4. TWO dead rows on one project plus a live externally-started bridge. The row
    # pass inserts a STOPPED card for each; the walk then resolves by PROJECT and adopts
    # the id it is handed. First-match handed it session-A's id, so the live bridge's
    # fields were written over session-A's record — and the next _persist rewrote that row
    # too, losing a resumable session. The walk must take an UNCLAIMED id instead.
    runner = _make_runner(runner_config)
    _stub_connect(monkeypatch)
    runner.persistence.state_store().save(
        {
            "iid-a": _row("alpha", pid=5001, resume_mode="standard", label="session-A"),
            "iid-b": _row("alpha", pid=5002, resume_mode="standard", label="session-B"),
        }
    )
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)

    class _ExternalPtr:
        pid = 9001
        proc_start = "1000"
        environment_id = "env_EXTERNAL"
        session_id = "session_EXTERNAL"

    monkeypatch.setattr("clauster.runner.pointers.pointer_for_project", lambda p: _ExternalPtr())
    monkeypatch.setattr("clauster.runner.pointers.is_live", lambda ptr: True)

    await runner.rediscover(persist=False)

    by_id = {i.instance_id: i for i in runner.list_instances() if i.project == "alpha"}
    # Both persisted sessions survive as their own STOPPED cards...
    assert by_id["iid-a"].status is InstanceStatus.STOPPED, "session-A was overwritten"
    assert by_id["iid-a"].label == "session-A"
    assert by_id["iid-b"].status is InstanceStatus.STOPPED
    # ...and the live external bridge is materialized under an id of its own.
    running = [i for i in by_id.values() if i.status is InstanceStatus.RUNNING]
    assert len(running) == 1, f"expected one RUNNING external bridge, got {running}"
    assert running[0].instance_id not in ("iid-a", "iid-b")
    assert running[0].bridge_pid == 9001


async def test_stopped_row_does_not_hide_a_live_detached_keeper(runner_config, monkeypatch):
    # The pty half of the same defect: with no Anthropic pointer, the walk's keeper-sidecar
    # leg is what re-manages a detached keeper that outlived the restart. Blocked by the same
    # guard, it never ran, so a LIVE keeper leaked behind a STOPPED card — and the new prune
    # then deleted that card, leaving the operator with no handle on a running bridge.
    #
    # Drives the REAL `_reattach_pty_from_sidecar`. This test used to stub that method and
    # assert only that it was CALLED, which is why it stayed green through the regression
    # it was written to prevent: resolving `saved` with `unclaimed_only=True` handed the
    # real method an empty dict, and it bailed on its own first line. A stub that ignores
    # the arguments cannot see a bug that lives in them.
    import json

    runner = _make_runner(runner_config)
    _stub_connect(monkeypatch)
    # The row's OWN bridge is dead -> the row pass cards it STOPPED and claims that id.
    runner.persistence.state_store().save({"iid-dead-pty": _row("alpha", pid=5003)})
    runner._log_dir.mkdir(parents=True, exist_ok=True)
    (runner._log_dir / "alpha-1700000000000-0.keeper.json").write_text(
        json.dumps(
            {
                "keeper_pid": 5555,
                "bridge_pid": 4242,
                "bridge_proc_start": 222.0,
                "state": "ready",
                "connect_url": "https://claude.ai/code/KEEPER",
            }
        )
    )
    # Live: the keeper and the bridge IT holds. The row's own pid stays dead.
    monkeypatch.setattr(
        "clauster.runner.procutil.is_live_bridge", lambda pid, proc_start=None: pid == 4242
    )
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda pid: pid == 5555)
    monkeypatch.setattr("clauster.runner.pointers.pointer_for_project", lambda path: None)

    await runner.rediscover(persist=False)

    live = [i for i in runner.list_instances() if i.status is InstanceStatus.RUNNING]
    assert len(live) == 1, "the live detached keeper leaked: unmanaged, no Stop, no observe"
    assert (live[0].keeper_pid, live[0].bridge_pid) == (5555, 4242)
    assert live[0].url == "https://claude.ai/code/KEEPER", "the sidecar's connect link is lost"
    # SF-4 still holds: the walk adopts a fresh id rather than the one the row pass carded.
    assert live[0].instance_id != "iid-dead-pty"


async def test_a_standard_row_ordered_first_does_not_hide_the_pty_keeper(
    runner_config, monkeypatch
):
    # Round-5 review (P1). The walk resolved the sidecar leg's modes by first match over
    # the project's rows, which is arbitrary in MODE as well as identity: a project with a
    # standard row AND a pty row could hand the leg the standard one, whose `resume_mode`
    # makes `_reattach_pty_from_sidecar` return before it ever globs a sidecar. Same leak
    # the leg exists to prevent, reached by a different route — and Resume would then
    # launch a duplicate pty bridge alongside the live one.
    import json

    runner = _make_runner(runner_config)
    _stub_connect(monkeypatch)
    # Order is the whole point: the standard row is first, so first-match finds it.
    runner.persistence.state_store().save(
        {
            "iid-standard": _row("alpha", pid=5001, resume_mode="standard"),
            "iid-pty": _row("alpha", pid=5002),
        }
    )
    runner._log_dir.mkdir(parents=True, exist_ok=True)
    (runner._log_dir / "alpha-1700000000000-0.keeper.json").write_text(
        json.dumps(
            {
                "keeper_pid": 5555,
                "bridge_pid": 4242,
                "bridge_proc_start": 222.0,
                "state": "ready",
                "connect_url": "https://claude.ai/code/KEEPER",
            }
        )
    )
    monkeypatch.setattr(
        "clauster.runner.procutil.is_live_bridge", lambda pid, proc_start=None: pid == 4242
    )
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda pid: pid == 5555)
    monkeypatch.setattr("clauster.runner.pointers.pointer_for_project", lambda path: None)

    await runner.rediscover(persist=False)

    live = [i for i in runner.list_instances() if i.status is InstanceStatus.RUNNING]
    assert len(live) == 1, "a standard row ordered first hid the pty row's live keeper"
    assert (live[0].keeper_pid, live[0].bridge_pid) == (5555, 4242)
    # The id must not come from the standard row either — that would write the keeper's
    # pids over a standard session's record.
    assert live[0].instance_id != "iid-standard"


async def test_keeper_reattach_does_not_card_itself_under_a_pid_less_rows_id(
    runner_config, monkeypatch
):
    # #1108. The keeper-sidecar leg picked its `instance_id` BEFORE the glob had found whose
    # keeper it was: first match over the project's UNCLAIMED pty rows. By the time the leg
    # runs, every row carrying a pid has been carded by the row pass, so the only unclaimed
    # rows left are pid-LESS ones — nothing about them can be correlated to the sidecar. The
    # live keeper was therefore carded under a pid-less session's identity, and the next
    # `_persist` rewrote that row with the keeper's pids, losing the session it described.
    runner = _make_runner(runner_config)
    _stub_connect(monkeypatch)
    runner.persistence.state_store().save(
        {
            # Pid-less (pre-#1115 / cleared): the row pass skips it, so it stays UNCLAIMED
            # and used to be the id the leg adopted.
            "iid-pidless": _row("alpha", pid=None, label="session-A"),
            # Dead by its own pids: carded STOPPED, hence claimed — this is what makes the
            # pid-less row the first (and only) unclaimed match.
            "iid-dead": _row("alpha", pid=5003, label="session-B"),
        }
    )
    runner._log_dir.mkdir(parents=True, exist_ok=True)
    (runner._log_dir / "alpha-1700000000000-0.keeper.json").write_text(
        json.dumps(
            {
                "keeper_pid": 5555,
                "bridge_pid": 4242,
                "bridge_proc_start": 222.0,
                "state": "ready",
                "connect_url": "https://claude.ai/code/KEEPER",
            }
        )
    )
    monkeypatch.setattr(
        "clauster.runner.procutil.is_live_bridge", lambda pid, proc_start=None: pid == 4242
    )
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda pid: pid == 5555)
    monkeypatch.setattr("clauster.runner.pointers.pointer_for_project", lambda path: None)

    await runner.rediscover()

    live = [i for i in runner.list_instances() if i.status is InstanceStatus.RUNNING]
    assert len(live) == 1, "the live detached keeper must still be re-managed"
    assert (live[0].keeper_pid, live[0].bridge_pid) == (5555, 4242)
    assert live[0].instance_id not in ("iid-pidless", "iid-dead")
    # ...the pid-less session's record survives the persist untouched...
    rows = runner.persistence.state_store().load()
    assert rows["iid-pidless"]["label"] == "session-A"
    assert rows["iid-pidless"].get("bridge_pid") is None, "the keeper's pids overwrote a row"
    # ...and it stays UNCARDED rather than offering a Resume that would spawn a second
    # keeper on the conversation this one may already be holding. The pid-less pass can no
    # longer see the keeper as unaccounted for (the fresh-id card holds it), so the walk
    # blocks the project's pty rows explicitly.
    assert runner.get_instance("iid-pidless") is None


async def test_second_restart_still_hides_the_pid_less_row_of_a_live_pty_session(
    runner_config, monkeypatch
):
    # The review catch on the fix above: `uncorrelated_keepers` only covers the restart
    # where the sidecar leg itself re-managed the keeper. One restart LATER the fresh-id
    # row reattaches by its persisted pids — the keeper is accounted for, that set is
    # empty, and the original pid-less row would be carded STOPPED, offering a Resume
    # that spawns a second keeper on the same `--continue` conversation. While ANY
    # managed pty instance is live in a project, its pid-less pty rows stay hidden.
    runner = _make_runner(runner_config)
    _stub_connect(monkeypatch)
    runner.persistence.state_store().save(
        {
            # The stale row of the very session the fresh-id card now drives.
            "iid-pidless": _row("alpha", pid=None, label="session-A"),
            # Restart-1's fresh-id card, persisted WITH the keeper's pids: the row pass
            # reattaches it directly, so the sidecar leg never runs for this project.
            "iid-fresh": _row("alpha", pid=4242, proc_start=222.0, label="session-A"),
        }
    )
    monkeypatch.setattr(
        "clauster.runner.procutil.is_live_bridge", lambda pid, proc_start=None: pid == 4242
    )
    monkeypatch.setattr(SessionRunner, "_recover_keeper_pid", lambda self, n, p, s: 5555)
    monkeypatch.setattr("clauster.runner.procutil.proc_create_time", lambda pid: 777.0)
    monkeypatch.setattr("clauster.runner.pointers.pointer_for_project", lambda path: None)

    await runner.rediscover()

    fresh = runner.get_instance("iid-fresh")
    assert fresh is not None and fresh.status is InstanceStatus.RUNNING
    assert runner.get_instance("iid-pidless") is None, (
        "a live managed pty instance must keep the project's pid-less pty rows hidden — "
        "carding one offers a duplicate --continue Resume"
    )


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


async def test_stopped_rows_survive_repeated_cold_starts(runner_config, monkeypatch):
    # THE #1115 regression test, and the reason it escaped #1088: one cold start always
    # looked right. The fold needed TWO. Cold start #1 materialized every row correctly and
    # then persisted each dead card's None back over the row's pids; cold start #2 read
    # those rows as pid-less (i.e. pre-#1088), sent them to the project-keyed pointer walk,
    # and rebuilt exactly ONE. A one-way ratchet — on the dogfood host it left 8 of 17 rows
    # visible, one per project, each the EARLIEST of its project's rows.
    rows = {f"iid-{n}": _row("alpha", pid=4000 + n, intentional_stop=True) for n in range(5)}
    _make_runner(runner_config).persistence.state_store().save(rows)
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda *a, **k: False)

    # Three fresh processes in a row: each must see all five, not just the first.
    for cold_start in range(3):
        runner = _make_runner(runner_config)
        await runner.rediscover(persist=True)
        got = {i.instance_id for i in runner.list_instances() if i.project == "alpha"}
        assert got == set(rows), f"cold start {cold_start + 1} folded to {sorted(got)}"
        assert all(
            i.status is InstanceStatus.STOPPED
            for i in runner.list_instances()
            if i.project == "alpha"
        )


async def test_pid_less_rows_left_by_the_old_ratchet_are_recovered(runner_config, monkeypatch):
    # The dogfood shape for #1115, measured on the live DB: 17 rows, 16 of them already
    # pid-less because the pre-fix ratchet had erased the pair, across 8 projects -> 8 cards,
    # one per project. Preserving the pair only stops FUTURE damage; nothing can backfill a
    # dead process's create-time, so these rows only come back if the reattach cards a
    # pid-less row per ROW instead of leaving them to the project-keyed walk.
    rows = {f"iid-{n}": _row("alpha", pid=None, intentional_stop=True) for n in range(6)}
    _make_runner(runner_config).persistence.state_store().save(rows)
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda *a, **k: False)

    runner = _make_runner(runner_config)
    await runner.rediscover(persist=True)

    got = {i.instance_id for i in runner.list_instances() if i.project == "alpha"}
    assert got == set(rows), f"expected all six damaged rows, got {sorted(got)}"
    # Each is resumable under its OWN id — that is what makes `clauster stop/resume <id>` work.
    for iid in rows:
        assert runner.resolve_bridge_id(iid) == iid


def _write_sidecar(runner, name: str, stamp: str, seq: int = 1, **fields) -> None:
    # `<name>-<ms>-<seq>` is the shape `_unique_log_path` actually writes, and the sweeps
    # anchor on both digit groups to reject a sibling project. A one-group stem is not a
    # filename clauster can produce, so building one here would test nothing real.
    log_dir = runner._log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"{name}-{stamp}-{seq}.keeper.json").write_text(json.dumps(fields))


async def test_has_unclaimed_live_keeper_detects_only_a_live_unheld_keeper(
    runner_config, monkeypatch
):
    # Drives the real sweep the pid-less pass gates on. Each sidecar below is rejected for a
    # different reason, so a regression in any one arm shows up as a wrong verdict.
    runner = _make_runner(runner_config)
    _write_sidecar(runner, "alpha", "1700000000001", state="starting", keeper_pid=1, bridge_pid=2)
    _write_sidecar(runner, "alpha", "1700000000002", state="ready", keeper_pid=True, bridge_pid=2)
    _write_sidecar(runner, "alpha", "1700000000003", state="ready", keeper_pid=3)  # no bridge pid
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda *a, **k: True)
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)

    # A non-dict sidecar is valid JSON but has no `.get` — it must be skipped, not raise
    # out of the sweep and take down startup.
    (runner._log_dir / "alpha-1700000000009-1.keeper.json").write_text("[]")

    assert runner._has_unclaimed_live_keeper("alpha", set()) is False, "no usable sidecar yet"

    # A ready sidecar naming a live keeper + live bridge is the one shape that counts.
    _write_sidecar(
        runner,
        "alpha",
        "1700000000004",
        state="ready",
        keeper_pid=9999,
        bridge_pid=4242,
        bridge_proc_start=12345.0,
    )
    assert runner._has_unclaimed_live_keeper("alpha", set()) is True

    # ...unless a LIVE tracked instance already holds that keeper — then it is accounted
    # for. Held pids come from live instances only: `stop()` leaves keeper_pid on a dead
    # card, and honouring that would let a stale pid mark a live keeper "claimed".
    assert runner._has_unclaimed_live_keeper("alpha", {9999}) is False

    # A dead bridge behind a live keeper does not count either.
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    assert runner._has_unclaimed_live_keeper("alpha", set()) is False


async def test_pid_less_standard_row_is_not_carded_while_the_pointer_bridge_lives(
    runner_config, monkeypatch
):
    # The standard-mode half of the same trap, and the subtler one: `_reattach_rows_with_pids`
    # claims a project when ANY row proves live — before it reads that row's mode — so one
    # live PTY row makes the walk skip the project entirely, and a pid-less STANDARD row
    # there still owns the pointer bridge nobody looked for. Carding it STOPPED offers a
    # Resume that overwrites the live bridge's pointer and orphans it.
    rows = {
        "iid-live-pty": _row("alpha", pid=5001),  # live pty row -> project is `row_claimed`
        "iid-pidless-std": _row("alpha", pid=None, resume_mode="standard"),
    }
    _make_runner(runner_config).persistence.state_store().save(rows)
    _stub_connect(monkeypatch)
    monkeypatch.setattr(
        "clauster.runner.procutil.is_live_bridge", lambda pid, _s=None, **k: pid == 5001
    )
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda *a, **k: True)

    class _Ptr:
        pid, proc_start, environment_id, session_id = 7777, "1000", "env_x", "session_x"

    monkeypatch.setattr(
        "clauster.pointers.pointer_for_project",
        lambda path: _Ptr() if path.name == "alpha" else None,
    )
    monkeypatch.setattr("clauster.pointers.is_live", lambda ptr: True)

    runner = _make_runner(runner_config)
    await runner.rediscover(persist=False)

    assert runner.get_instance("iid-live-pty") is not None
    assert runner.get_instance("iid-pidless-std") is None, (
        "a pid-less standard row must stay uncarded while its pointer bridge is live"
    )

    # With the pointer bridge gone, the same row cards normally.
    monkeypatch.setattr("clauster.pointers.is_live", lambda ptr: False)
    runner2 = _make_runner(runner_config)
    await runner2.rediscover(persist=False)
    assert runner2.get_instance("iid-pidless-std") is not None


async def test_pid_less_pty_row_is_not_carded_while_a_live_keeper_is_unclaimed(
    runner_config, monkeypatch
):
    # The trap the pid-less pass must not fall into. `rediscover`'s pointer walk SKIPS a
    # project that already has a live row, before it ever reads the pointer or the keeper
    # sidecar — so on that project nothing has looked for a pid-less row's process. A pty
    # row whose detached keeper is still alive must NOT get a resumable STOPPED card: the
    # Resume would spawn a SECOND keeper on the same `--continue` conversation.
    rows = {
        "iid-live": _row("alpha", pid=5001),  # keeps the project in `row_claimed`
        "iid-pidless": _row("alpha", pid=None),
    }
    _make_runner(runner_config).persistence.state_store().save(rows)
    _stub_connect(monkeypatch)
    monkeypatch.setattr(
        "clauster.runner.procutil.is_live_bridge", lambda pid, _s=None, **k: pid == 5001
    )
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda *a, **k: True)

    runner = _make_runner(runner_config)
    monkeypatch.setattr(SessionRunner, "_has_unclaimed_live_keeper", lambda self, name, held: True)
    await runner.rediscover(persist=False)

    assert runner.get_instance("iid-live") is not None
    assert runner.get_instance("iid-pidless") is None, (
        "a pid-less pty row must stay uncarded while a live keeper is unaccounted for"
    )

    # With the keeper gone AND no live pty session left in the project, the same row cards
    # normally. Both conditions matter: while any managed pty instance is live, a pid-less
    # pty row stays hidden regardless — its `--continue` Resume could only duplicate or
    # steal the live conversation (the second-restart review catch).
    runner2 = _make_runner(runner_config)
    monkeypatch.setattr(
        SessionRunner, "_has_unclaimed_live_keeper", lambda self, name, held: False
    )
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    await runner2.rediscover(persist=False)
    assert runner2.get_instance("iid-pidless") is not None


async def test_pid_less_pass_leaves_a_pid_bearing_row_to_the_row_pass(runner_config, monkeypatch):
    # The pid-less pass must never card a row that HAS a pair — that row's verdict belongs to
    # `_reattach_rows_with_pids`, which judges it on liveness. Reachable when the row pass
    # skipped its insert across one of its own awaits (its `iid in _instances` re-check), or
    # when a concurrent refresh introduced the row mid-`rediscover`.
    rows = {"iid-paired": _row("alpha", pid=6001)}
    _make_runner(runner_config).persistence.state_store().save(rows)
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda *a, **k: False)

    # Neuter BOTH earlier card sources, so the pid-less pass is the only thing that could
    # card this row — otherwise the walk's `_stopped_from_persisted` fallback cards it and
    # the assertion proves nothing about the pass under test.
    async def _no_rows(self, *a, **k):
        return set(), set()

    monkeypatch.setattr(SessionRunner, "_reattach_rows_with_pids", _no_rows)
    monkeypatch.setattr(SessionRunner, "_stopped_from_persisted", lambda self, name: None)

    runner = _make_runner(runner_config)
    await runner.rediscover(persist=False)

    assert runner.get_instance("iid-paired") is None, (
        "a row carrying a pid must be left to the row pass, not carded here"
    )


async def test_mode_less_legacy_row_is_swept_by_both_mechanisms_on_a_pty_host(
    runner_config, monkeypatch
):
    # `_saved_modes` coerces a row with NO recorded resume_mode to the host's configured
    # `claude.launch_mode` — a fact about the deployment, not the row. Routing the liveness
    # sweep on that guess meant a pre-#1088 row on a `launch_mode: pty` host coerced to
    # "pty", never had its pointer read, and got carded STOPPED while its STANDARD bridge
    # was live (such a row predates pty, so it can only have been standard). The default
    # fixture host is `standard`, where the coercion happens to land on the right check —
    # which is exactly why this needs a pty-configured host to show up.
    config, claude_json = runner_config
    monkeypatch.setattr(config.claude, "launch_mode", "pty")
    rows = {
        "iid-live-pty": _row("alpha", pid=5001),  # live row -> project is `row_claimed`
        "iid-legacy": {"project_name": "alpha", "label": "alpha"},  # no resume_mode at all
    }
    _make_runner(runner_config).persistence.state_store().save(rows)
    _stub_connect(monkeypatch)
    monkeypatch.setattr(
        "clauster.runner.procutil.is_live_bridge", lambda pid, _s=None, **k: pid == 5001
    )
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda *a, **k: True)

    class _Ptr:
        pid, proc_start, environment_id, session_id = 7777, "1000", "env_x", "session_x"

    monkeypatch.setattr(
        "clauster.pointers.pointer_for_project",
        lambda path: _Ptr() if path.name == "alpha" else None,
    )
    monkeypatch.setattr("clauster.pointers.is_live", lambda ptr: True)

    runner = _make_runner(runner_config)
    await runner.rediscover(persist=False)

    assert runner.get_instance("iid-legacy") is None, (
        "a mode-less row must be swept by the pointer check too, not just the keeper one"
    )


async def test_a_mode_with_no_sweep_blocks_rather_than_going_unswept(runner_config):
    # Fail-CLOSED for an unknown resume_mode. The allowlist dispatch sweeps "pty" via keeper
    # sidecars and "standard" via the pointer; a third mode matches neither, and without an
    # explicit raise it would get NO sweep yet still be carded — `_sweep_modes` puts it in
    # `pending`, so the `unswept` guard clears, and nothing puts it in `blocked`. That is the
    # duplicate-Resume hazard this pass exists to prevent, so it must block instead.
    runner = _make_runner(runner_config)
    config, _ = runner_config

    blocked = runner._modes_with_an_unclaimed_live_bridge(
        {"alpha": (frozenset({"quantum"}), config.projects_root / "alpha")}, set(), set()
    )

    assert ("alpha", "quantum") in blocked, "a mode with no sweep must block, not pass"


async def test_a_sidecar_with_a_nonpositive_pid_is_skipped_not_fatal(runner_config, monkeypatch):
    # `psutil.Process(-1)` raises ValueError, which `is_keeper_process`'s except-tuple does
    # NOT catch — and a sidecar is an on-disk file that can hold one. Skipping it must be
    # local to that sidecar: a live keeper named by a LATER sidecar still has to be found.
    runner = _make_runner(runner_config)
    _write_sidecar(runner, "alpha", "1700000000001", state="ready", keeper_pid=-1, bridge_pid=-2)
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda *a, **k: True)
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)

    assert runner._has_unclaimed_live_keeper("alpha", set()) is False

    _write_sidecar(
        runner,
        "alpha",
        "1700000000002",
        state="ready",
        keeper_pid=9999,
        bridge_pid=4242,
        bridge_proc_start=12345.0,
    )
    assert runner._has_unclaimed_live_keeper("alpha", set()) is True, (
        "a bad sidecar must not mask a good one"
    )


async def test_a_sibling_projects_keeper_is_never_read_as_this_projects(
    runner_config, monkeypatch
):
    # `glob(f"{name}-*.keeper.json")` was an UNANCHORED prefix match and PROJECT_NAME_RE
    # allows `-`, so project `alpha` also read sibling `alpha-staging`'s sidecars. Nothing
    # downstream re-pinned the candidate to a project, so a sibling's LIVE keeper could be
    # adopted as alpha's RUNNING instance — and stop()/poll_once would then reap another
    # project's bridge. `_latest_debug_log_for` has always anchored the `<name>-<ms>-<seq>`
    # stem for exactly this reason; the four `.keeper.json` sites did not.
    runner = _make_runner(runner_config)
    _write_sidecar(
        runner,
        "alpha-staging",
        "1700000000001",
        state="ready",
        keeper_pid=9999,
        bridge_pid=4242,
        bridge_proc_start=12345.0,
    )
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda *a, **k: True)
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)

    assert runner._keeper_sidecars_for("alpha") == []
    assert runner._has_unclaimed_live_keeper("alpha", set()) is False
    assert runner._recover_keeper_pid("alpha", 4242, None) is None

    # Differential control: the sibling is a real project with a real live keeper, so
    # querying under its OWN name must still find it. Without this the assertions above
    # would also pass if the anchor rejected everything.
    assert len(runner._keeper_sidecars_for("alpha-staging")) == 1
    assert runner._has_unclaimed_live_keeper("alpha-staging", set()) is True
    assert runner._recover_keeper_pid("alpha-staging", 4242, None) == 9999


def test_keeper_sidecars_are_anchored_to_the_real_spawn_stem(runner_config):
    # The anchor is the same two-digit-group shape `_unique_log_path` writes
    # (`<name>-<ms>-<seq>`), so a stem clauster could never have produced is not read as a
    # sidecar of this project — nor are the `.log` / `.keeper.log` kin sharing that stem.
    runner = _make_runner(runner_config)
    runner._log_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "alpha-1700000000001-1.keeper.json",  # the real shape — the only match
        "alpha-1700000000001.keeper.json",  # one digit group: not a name clauster writes
        "alpha-staging-1700000000001-1.keeper.json",  # sibling project
        "alphabet-1700000000001-1.keeper.json",  # prefix collision without a separator
        "alpha-1700000000001-1.keeper.log",  # spawn-set kin, not the sidecar
        "alpha-nope-1.keeper.json",  # non-numeric ms
    ):
        (runner._log_dir / name).write_text("{}")

    assert [p.name for p in runner._keeper_sidecars_for("alpha")] == [
        "alpha-1700000000001-1.keeper.json"
    ]


async def test_forget_refuses_a_persisted_only_row_with_a_live_bridge(runner_config, monkeypatch):
    # All three liveness gates sat inside `if instance is not None`, so a row present only in
    # _persisted was pruned with NO liveness check at all. That is not a corner case: it is
    # the branch MOST likely to hold a live process, because rediscover deliberately leaves a
    # row uncarded exactly when its bridge/keeper is alive but untracked. Forgetting it
    # orphaned a live bridge and deleted the only record of it.
    runner = _make_runner(runner_config)
    runner.persistence.state_store().save({"iid-ghost": _row("alpha", pid=4242)})
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)

    assert runner.get_instance("iid-ghost") is None  # persisted only — never materialized

    with pytest.raises(InstanceStillLive):
        await runner.forget("iid-ghost")

    assert "iid-ghost" in runner.persistence.state_store().load()  # refusal kept the row


async def test_forget_refuses_a_persisted_only_row_with_a_live_keeper(runner_config, monkeypatch):
    # The keeper half of the same gate: a pty row can have no bridge pid and still own a live
    # keeper holding the terminal.
    runner = _make_runner(runner_config)
    runner.persistence.state_store().save(
        {"iid-ghost": _row("alpha", pid=None, keeper_pid=7777, keeper_proc_start=900.0)}
    )
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    monkeypatch.setattr("clauster.runner.procutil.is_live_keeper", lambda pid, start: True)

    with pytest.raises(InstanceStillLive):
        await runner.forget("iid-ghost")

    assert "iid-ghost" in runner.persistence.state_store().load()


async def test_forget_is_not_stranded_by_a_recycled_keeper_pid(runner_config, monkeypatch):
    # The reason the keeper gate is `is_live_keeper` and not `proc_create_time is not None`.
    # The latter answers "is ANY process alive at this pid" — and a persisted-only row can
    # predate a reboot, so its keeper_pid has a real chance of having been recycled onto
    # something unrelated. Since forget never kills, a false "still live" strands the record
    # with no operator path out short of editing the state DB.
    runner = _make_runner(runner_config)
    runner.persistence.state_store().save({"iid-ghost": _row("alpha", pid=None, keeper_pid=7777)})
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    # Alive, but not our keeper — exactly the recycled-pid shape.
    monkeypatch.setattr("clauster.runner.procutil.proc_create_time", lambda pid: 12345.0)
    monkeypatch.setattr("clauster.runner.procutil.is_live_keeper", lambda pid, start: False)

    await runner.forget("iid-ghost")  # must not be refused

    assert "iid-ghost" not in runner.persistence.state_store().load()


# ---------------------------------------------------------------------------
# Bug 1178 — forget's keeper gate had no PID-reuse defense
# ---------------------------------------------------------------------------


def _pretend_a_live_keeper_holds_the_pid(monkeypatch, *, create_time: float) -> None:
    """Make every pid look like a LIVE keeper started at ``create_time``.

    Faked at ``psutil.Process`` rather than at ``procutil.is_live_keeper``, so these tests
    run the real predicate: what is under test is whether ``forget`` hands it the stored
    start time at all, and stubbing the predicate itself would pass with the value dropped.
    """
    import psutil

    class _FakeKeeper:
        def __init__(self, pid):
            pass

        def status(self):
            return psutil.STATUS_RUNNING

        def cmdline(self):
            return ["/usr/bin/python3", "-m", "clauster.pty_keeper", "--sidecar", "/tmp/k.json"]

        def create_time(self):
            return create_time

    monkeypatch.setattr("clauster.procutil.psutil.Process", _FakeKeeper)


async def test_forget_allows_a_row_whose_keeper_pid_now_holds_a_different_keeper(
    runner_config, monkeypatch
):
    # THE #1178 regression test. The row's keeper_pid is held by a live process that IS a
    # keeper by cmdline — just not OURS, which the persisted create-time proves. Before the
    # column existed the cmdline gate answered "still live", and since forget never kills,
    # the record was stranded with no operator path out short of editing the state DB.
    runner = _make_runner(runner_config)
    runner.persistence.state_store().save(
        {"iid-ghost": _row("alpha", pid=None, keeper_pid=7777, keeper_proc_start=900.0)}
    )
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    # A keeper by cmdline, but started at a different time — a DIFFERENT keeper on that pid.
    _pretend_a_live_keeper_holds_the_pid(monkeypatch, create_time=5000.0)

    await runner.forget("iid-ghost")  # must not be refused

    assert "iid-ghost" not in runner.persistence.state_store().load()


async def test_forget_still_refuses_when_the_keeper_start_time_matches(runner_config, monkeypatch):
    # The other half of the same gate, and the control for the test above: with the create-time
    # MATCHING, this is our keeper and the forget must still fail closed. Only the start time
    # differs between the two tests, so it is that value deciding — not the cmdline.
    runner = _make_runner(runner_config)
    runner.persistence.state_store().save(
        {"iid-ghost": _row("alpha", pid=None, keeper_pid=7777, keeper_proc_start=900.0)}
    )
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    _pretend_a_live_keeper_holds_the_pid(monkeypatch, create_time=900.0)

    with pytest.raises(InstanceStillLive):
        await runner.forget("iid-ghost")

    assert "iid-ghost" in runner.persistence.state_store().load()


async def test_forget_gate_degrades_to_cmdline_for_a_pre_1178_row(runner_config, monkeypatch):
    # Old-row compatibility. A row written before `keeper_proc_start` existed carries
    # keeper_pid and no start time; the gate must behave exactly as it did then — cmdline +
    # alive — so an upgrade neither orphans that keeper nor makes the row unforgettable.
    # Identical to the test above except that the row stores no start time, and the live
    # process's create-time is one that WOULD have mismatched.
    runner = _make_runner(runner_config)
    runner.persistence.state_store().save({"iid-legacy": _row("alpha", pid=None, keeper_pid=7777)})
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    _pretend_a_live_keeper_holds_the_pid(monkeypatch, create_time=5000.0)

    with pytest.raises(InstanceStillLive):
        await runner.forget("iid-legacy")

    assert "iid-legacy" in runner.persistence.state_store().load()


async def test_keeper_pid_and_start_time_are_persisted_and_reloaded_together(
    runner_config, monkeypatch
):
    # The pair has to survive the round-trip or the gate above has nothing to compare. A pid
    # persisted WITHOUT its start time is the pre-#1178 state; a start time persisted without
    # its pid is meaningless.
    runner = _make_runner(runner_config)
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    runner._instances["iid-pty"] = RemoteControlInstance(
        instance_id="iid-pty",
        project="alpha",
        label="alpha",
        resume_mode="pty",
        status=InstanceStatus.STOPPED,
        bridge_pid=4242,
        bridge_proc_start=222.0,
        keeper_pid=5555,
        keeper_proc_start=333.0,
    )

    await runner._persist()

    row = runner.persistence.state_store().load()["iid-pty"]
    assert (row["keeper_pid"], row["keeper_proc_start"]) == (5555, 333.0)


async def test_reattach_records_the_keeper_start_time_with_its_pid(runner_config, monkeypatch):
    # A row reattached at startup takes its keeper pid from the sidecar, so the create-time
    # has to be snapshotted at that same classification — otherwise every reattached pty row
    # would carry a bare pid and the defense would be inert exactly where the stale-pid risk
    # is highest.
    runner = _make_runner(runner_config)
    _stub_connect(monkeypatch)
    runner.persistence.state_store().save({"iid-pty": _row("alpha", pid=5002)})
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    monkeypatch.setattr(SessionRunner, "_recover_keeper_pid", lambda self, n, p, s: 5555)
    monkeypatch.setattr("clauster.runner.procutil.proc_create_time", lambda pid: 777.0)

    await runner.rediscover(persist=False)

    inst = runner.get_instance("iid-pty")
    assert inst is not None
    assert (inst.keeper_pid, inst.keeper_proc_start) == (5555, 777.0)


async def test_pointer_walk_reattach_records_the_keeper_start_time_too(runner_config, monkeypatch):
    # The pointer-walk leg — a pre-instance-keyed row with NO persisted pid whose bridge is
    # still live at the Anthropic pointer — recovered only the keeper PID, leaving that row's
    # forget gate degraded to cmdline-only: the one reattach path where the pair defense
    # stayed inert. The create-time must be snapshotted here exactly as the instance-keyed
    # leg above does.
    from clauster import pointers

    runner = _make_runner(runner_config)
    runner.persistence.state_store().save({"iid-pty": _row("alpha", pid=None)})
    ptr = pointers.BridgePointer(
        pid=6001,
        proc_start="123",
        source="test",
        environment_id="env_PTY",
        session_id="session_PTY",
    )
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    monkeypatch.setattr("clauster.runner.pointers.pointer_for_project", lambda *a, **k: ptr)
    monkeypatch.setattr(SessionRunner, "_recover_keeper_pid", lambda self, n, p, s: 6666)
    monkeypatch.setattr("clauster.runner.procutil.proc_create_time", lambda pid: 888.0)

    await runner.rediscover(persist=False)

    inst = runner.get_instance("iid-pty")
    assert inst is not None
    assert (inst.keeper_pid, inst.keeper_proc_start) == (6666, 888.0)


async def test_resync_replaces_the_keeper_pair_together(runner_config, monkeypatch):
    # A cross-process resync adopts a NEW keeper pid. Leaving the previous generation's
    # start time beside it would be worse than carrying none: the comparison then fails for
    # a keeper that is genuinely alive, and forget would drop the record of a running process.
    runner = _make_runner(runner_config)
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    runner._instances["iid-pty"] = RemoteControlInstance(
        instance_id="iid-pty",
        project="alpha",
        label="alpha",
        resume_mode="pty",
        status=InstanceStatus.STOPPED,
        bridge_pid=4401,
        bridge_proc_start=100.0,
        keeper_pid=1111,
        keeper_proc_start=111.0,  # the DEAD generation's keeper
    )
    runner.persistence.state_store().save(
        {"iid-pty": _row("alpha", pid=4402, proc_start=200.0)}  # another process's live bridge
    )
    monkeypatch.setattr(
        "clauster.runner.procutil.is_live_bridge", lambda pid, _s=None: pid == 4402
    )
    _stub_connect(monkeypatch)
    monkeypatch.setattr(SessionRunner, "_recover_keeper_pid", lambda self, n, p, s: 2222)
    monkeypatch.setattr("clauster.runner.procutil.proc_create_time", lambda pid: 222.0)

    await runner.poll_once()

    inst = runner.get_instance("iid-pty")
    assert inst is not None
    assert (inst.keeper_pid, inst.keeper_proc_start) == (2222, 222.0), (
        "the keeper pid and its start time must be replaced as a pair"
    )


async def test_a_standard_resync_clears_the_keeper_pair(runner_config, monkeypatch):
    # A standard bridge has no keeper. Both halves must go, not just the pid: a leftover
    # start time paired with a None pid is dead weight the gate would never consult, and the
    # asymmetry is how a stale value survives into the next pty generation.
    runner = _make_runner(runner_config)
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    runner._instances["iid-std"] = RemoteControlInstance(
        instance_id="iid-std",
        project="alpha",
        label="alpha",
        resume_mode="standard",
        status=InstanceStatus.STOPPED,
        bridge_pid=4401,
        bridge_proc_start=100.0,
        keeper_pid=1111,
        keeper_proc_start=111.0,
    )
    runner.persistence.state_store().save(
        {"iid-std": _row("alpha", pid=4402, proc_start=200.0, resume_mode="standard")}
    )
    monkeypatch.setattr(
        "clauster.runner.procutil.is_live_bridge", lambda pid, _s=None: pid == 4402
    )
    _stub_connect(monkeypatch)

    await runner.poll_once()

    inst = runner.get_instance("iid-std")
    assert inst is not None
    assert (inst.keeper_pid, inst.keeper_proc_start) == (None, None)


async def test_forget_still_prunes_a_dead_persisted_only_row(runner_config, monkeypatch):
    # The control that keeps the gate honest: tightening forget must not break its actual
    # job. A persisted-only row whose processes are gone is exactly what forget exists to
    # clear, and it must still go.
    runner = _make_runner(runner_config)
    runner.persistence.state_store().save({"iid-ghost": _row("alpha", pid=4242, keeper_pid=7777)})
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    # `is_keeper_process`, not `proc_create_time` — the branch stopped calling the latter
    # when the keeper gate became cmdline-gated, and a stub on it would leave the keeper
    # half of this control decided by real psutil against whatever holds pid 7777.
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda pid: False)

    await runner.forget("iid-ghost")  # must not raise

    assert "iid-ghost" not in runner.persistence.state_store().load()


async def test_forget_is_not_stranded_by_a_negative_pid_on_a_persisted_only_row(runner_config):
    # Review catch on this branch. `_row_int` admits ANY non-bool int, negatives included,
    # and the new gate hands that straight to psutil — which raises ValueError, not
    # NoSuchProcess, below zero. Hardening only `is_keeper_process`/`is_bridge_process` left
    # `is_live_bridge` and `proc_create_time` raising, and those are the two this branch
    # actually calls: the row would 500 on every forget and could never be removed by any
    # supported path. Reachable without hand-editing, because `_apply_pty_info` folds a
    # sidecar's pid with no `> 0` gate and the pid columns are persisted.
    # Real procutil here on purpose — a stub would not reproduce psutil's raise.
    runner = _make_runner(runner_config)
    runner.persistence.state_store().save(
        {"iid-ghost": _row("alpha", pid=-1, keeper_pid=-2)},
    )

    await runner.forget("iid-ghost")  # must not raise ValueError

    assert "iid-ghost" not in runner.persistence.state_store().load()


async def test_forget_ignores_a_non_int_pid_on_a_persisted_only_row(runner_config, monkeypatch):
    # The new gate reads pids straight off a persisted row, so it has to tolerate junk there:
    # `_row_int` degrades a non-int to None instead of letting it reach psutil, which would
    # raise out of forget and make the row unforgettable. A string is what actually exercises
    # that — a bool cannot reach this branch, because the store coerces `true` to `1` on
    # round-trip (probed, not assumed), so `_row_int`'s bool arm guards the in-memory path.
    runner = _make_runner(runner_config)
    runner.persistence.state_store().save(
        {"iid-ghost": {"project_name": "alpha", "bridge_pid": "one", "keeper_pid": "seven"}}
    )
    monkeypatch.setattr(
        "clauster.runner.procutil.is_live_bridge",
        lambda *a, **k: pytest.fail("a non-int pid must never reach the liveness check"),
    )
    monkeypatch.setattr(
        "clauster.runner.procutil.is_keeper_process",
        lambda pid: pytest.fail("a non-int keeper pid must never reach the liveness check"),
    )

    await runner.forget("iid-ghost")

    assert "iid-ghost" not in runner.persistence.state_store().load()


async def test_a_sweep_that_raises_blocks_that_project_instead_of_killing_startup(
    runner_config, monkeypatch
):
    # The sweep runs inside a to_thread on `rediscover`, which the web app awaits during
    # lifespan STARTUP with no handler of its own — so an escaping exception takes the whole
    # service down rather than losing one project's sweep. It must degrade to BLOCKED, which
    # also keeps the fail-closed posture (rows hidden, not carded unswept).
    rows = {"iid-a": _row("alpha", pid=None), "iid-b": _row("alpha", pid=None)}
    _make_runner(runner_config).persistence.state_store().save(rows)
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda *a, **k: False)

    def _boom(self, name, held):
        raise RuntimeError("psutil blew up")

    monkeypatch.setattr(SessionRunner, "_has_unclaimed_live_keeper", _boom)

    runner = _make_runner(runner_config)
    await runner.rediscover(persist=False)  # must not raise

    # The walk still cards one row per project; the pid-less pass blocks the rest.
    carded = [i for i in runner.list_instances() if i.project == "alpha"]
    assert len(carded) == 1, f"a failed sweep must block, not card: {carded}"


async def test_row_arriving_during_the_sweep_is_deferred_not_carded(runner_config, monkeypatch):
    # The fail-closed arm. A row that appears while the sweep is awaiting was never swept,
    # so carding it would bypass the liveness gate entirely. It must be deferred to the next
    # start. Mode-exact on purpose: a project swept only for "pty" never had its pointer
    # read, so a standard row arriving late must not pass on the project's name alone.
    # TWO pid-less pty rows on purpose: the project-keyed walk cards exactly one of them, so
    # the other reaches the pid-less pass and `pending` is non-empty. With a single row the
    # walk cards it, `pending` is empty, and the `if pending else set()` short-circuit means
    # the sweep never runs at all — the test would pass without exercising anything.
    rows = {"iid-pty-a": _row("alpha", pid=None), "iid-pty-b": _row("alpha", pid=None)}
    _make_runner(runner_config).persistence.state_store().save(rows)
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda *a, **k: False)

    runner = _make_runner(runner_config)

    def _inject(self, pending, held_keepers, held_pids):
        # Land a standard row for the SAME project mid-sweep: `alpha` is in `pending`, but
        # only for "pty", so its pointer was never consulted.
        self._persisted = {
            **self._persisted,
            "iid-late-std": {
                "project_name": "alpha",
                "label": "alpha",
                "resume_mode": "standard",
            },
        }
        return set()

    monkeypatch.setattr(SessionRunner, "_modes_with_an_unclaimed_live_bridge", _inject)
    await runner.rediscover(persist=False)

    assert runner.get_instance("iid-pty-a") is not None, "the swept rows still card"
    assert runner.get_instance("iid-pty-b") is not None
    assert runner.get_instance("iid-late-std") is None, (
        "a row whose mode was never swept must be deferred, not carded"
    )


async def test_stopped_rows_keep_their_pair_but_are_never_read_as_live(runner_config, monkeypatch):
    # #1115. A dead card carries no pids, but its ROW must keep the (pid, proc_start) PAIR:
    # that pair is what marks the row instance-keyed. Writing the card's None back made the
    # row look pre-#1088 on the NEXT cold start, so it fell to the project-keyed pointer
    # walk — one card per project — and every stopped session but the earliest vanished.
    # Safety is unchanged because liveness is judged on the PAIR: the recycled pid this
    # once guarded against is rejected by the start-time compare, asserted below.
    runner = _make_runner(runner_config)
    runner.persistence.state_store().save({"iid-a": _row("alpha", pid=8001, proc_start=222.5)})
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    await runner.rediscover(persist=True)

    saved = runner.persistence.state_store().load()["iid-a"]
    assert saved.get("bridge_pid") == 8001
    assert saved.get("bridge_proc_start") == 222.5
    # The CARD still carries none, so nothing in-process can act on a stale pid.
    card = runner.get_instance("iid-a")
    assert card is not None
    assert card.bridge_pid is None and card.bridge_proc_start is None
    # And the pair is only ever consulted through the real start-time compare: pid 8001
    # reused by an unrelated bridge has a different create_time, so it stays dead.
    monkeypatch.setattr(
        "clauster.runner.procutil.is_live_bridge",
        lambda pid, start=None, **k: pid == 8001 and start == 222.5,
    )
    monkeypatch.setattr("clauster.procutil.is_live_process", lambda *a, **k: False)
    recycled = _make_runner(runner_config)
    recycled.persistence.state_store().save(
        {"iid-a": {**saved, "bridge_proc_start": 999.9}}  # same pid, new process generation
    )
    await recycled.rediscover(persist=False)
    reread = recycled.get_instance("iid-a")
    assert reread is not None
    assert reread.status is InstanceStatus.STOPPED, "a recycled pid must not resurrect the row"


async def test_stopped_row_without_a_proc_start_still_drops_its_pid(runner_config, monkeypatch):
    # The other half of #1115's trade. A bare pid has no start-time to compare, so
    # `is_live_bridge` degrades to "alive + bridge cmdline" and a reused pid running any
    # bridge would read as this one. That row cannot be made reuse-proof, so it is still
    # cleared — it keeps folding, which is the safe direction.
    runner = _make_runner(runner_config)
    runner.persistence.state_store().save({"iid-a": _row("alpha", pid=8001, proc_start=None)})
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
    monkeypatch.setattr("clauster.runner.procutil.bridge_ancestor", lambda pid, **k: 990001)
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
    monkeypatch.setattr("clauster.runner.procutil.bridge_ancestor", lambda pid, **k: 990001)
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
    monkeypatch.setattr("clauster.runner.procutil.bridge_ancestor", lambda pid, **k: 990001)
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
    (runner._log_dir / "alpha-333-1.keeper.json").write_text(
        json.dumps(
            {"bridge_pid": 9999, "state": "ready", "connect_url": "https://claude.ai/code/OTHER"}
        )
    )
    (runner._log_dir / "alpha-222-1.keeper.json").write_text(
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
    (runner._log_dir / "alpha-444-1.keeper.json").write_text(
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
    (runner._log_dir / "alpha-555-1.keeper.json").write_text(
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
    monkeypatch.setattr("clauster.runner.procutil.bridge_ancestor", lambda pid, **k: None)
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
    # Readiness evidence for the ADOPTED generation: since MF-3 the re-sync recomputes the
    # connect facts instead of keeping the dead generation's, and RUNNING requires them.
    _stub_connect(monkeypatch)
    await runner.poll_once()

    inst = runner.get_instance("iid-a")
    assert inst is not None
    assert inst.bridge_pid == 4402, "must take the live pids another process recorded"
    assert inst.bridge_proc_start == 200.0
    assert inst.status is InstanceStatus.RUNNING


async def test_resync_replaces_the_dead_generations_connect_facts(runner_config, monkeypatch):
    # MF-3. Taking the live pids while keeping the previous generation's url/environment_id
    # published a RUNNING bridge whose connect link pointed into an environment that no
    # longer exists — permanently, since nothing else on the poll path refreshes them.
    runner = _make_runner(runner_config)
    store = runner.persistence.state_store()
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    runner._instances["iid-a"] = RemoteControlInstance(
        instance_id="iid-a",
        project="alpha",
        label="alpha",
        status=InstanceStatus.RUNNING,
        bridge_pid=4401,
        bridge_proc_start=100.0,
        environment_id="env_OLD",
        url="https://claude.ai/code?environment=env_OLD",
        starter_session_id="session_OLD",
    )
    store.save({"iid-a": _row("alpha", pid=4402, proc_start=200.0)})
    monkeypatch.setattr(
        "clauster.runner.procutil.is_live_bridge", lambda pid, _s=None: pid == 4402
    )
    # No pointer and no ready sidecar => no connect evidence for the new generation.
    monkeypatch.setattr(SessionRunner, "_connect_facts_for", lambda *a, **k: {})

    await runner.poll_once()

    inst = runner.get_instance("iid-a")
    assert inst is not None
    assert inst.bridge_pid == 4402
    assert inst.environment_id is None, "must not keep the dead generation's environment"
    assert inst.url is None, "a link into a dead environment is worse than none"
    assert inst.starter_session_id is None
    # Liveness is not usability: without connect evidence this is STARTING, not RUNNING.
    assert inst.status is InstanceStatus.STARTING


async def test_resync_keeps_a_pid_published_during_the_liveness_probe(runner_config, monkeypatch):
    # MF-2, the core case. The `ours` snapshot is taken BEFORE the liveness thread hop. If a
    # resume() publishes a fresh live pid during that hop, the pre-hop snapshot says "ours is
    # dead" and the row's pid would overwrite the brand-new one — orphaning a bridge that
    # nothing can stop, because stop() would then signal the wrong pid.
    runner = _make_runner(runner_config)
    store = runner.persistence.state_store()
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    runner._instances["iid-a"] = RemoteControlInstance(
        instance_id="iid-a",
        project="alpha",
        label="alpha",
        status=InstanceStatus.STOPPED,
        bridge_pid=4401,  # ours at snapshot: dead
        bridge_proc_start=100.0,
    )
    store.save({"iid-a": _row("alpha", pid=4402, proc_start=200.0)})  # another process's live
    _stub_connect(monkeypatch)

    def _is_live(pid, _s=None):
        # Stand in for resume() landing mid-probe and publishing its fresh live pid.
        inst = runner._instances.get("iid-a")
        if inst is not None and inst.bridge_pid == 4401:
            inst.bridge_pid, inst.bridge_proc_start = 7777, 300.0
        return pid in (4402, 7777)

    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", _is_live)

    await runner.poll_once()

    inst = runner.get_instance("iid-a")
    assert inst is not None
    assert inst.bridge_pid == 7777, "clobbered a live pid published during the probe"
    assert inst.bridge_proc_start == 300.0


async def test_resync_skips_an_instance_replaced_during_the_probe(runner_config, monkeypatch):
    # A lock-holding spawn()/adopt() can register a NEW object under the same id while the
    # probe is in flight. Mutating it would edit an object the HTTP caller was already handed.
    runner = _make_runner(runner_config)
    store = runner.persistence.state_store()
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    runner._instances["iid-a"] = RemoteControlInstance(
        instance_id="iid-a",
        project="alpha",
        label="alpha",
        status=InstanceStatus.STOPPED,
        bridge_pid=4401,
        bridge_proc_start=100.0,
    )
    store.save({"iid-a": _row("alpha", pid=4402, proc_start=200.0)})
    _stub_connect(monkeypatch)

    def _is_live(pid, _s=None):
        if runner._instances["iid-a"].bridge_pid == 4401:
            runner._instances["iid-a"] = RemoteControlInstance(
                instance_id="iid-a",
                project="alpha",
                label="alpha",
                status=InstanceStatus.RUNNING,
                bridge_pid=5555,
                bridge_proc_start=400.0,
            )
        return pid in (4402, 5555)

    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", _is_live)

    await runner.poll_once()

    inst = runner.get_instance("iid-a")
    assert inst is not None
    assert inst.bridge_pid == 5555, "overwrote the object a lock holder had just registered"


async def test_resync_skips_a_row_whose_project_is_no_longer_discovered(
    runner_config, monkeypatch, caplog
):
    # Without a Project there is nothing to attribute the connect facts to, so the row is
    # left alone rather than adopted half-populated.
    #
    # The pid assertion ALONE cannot tell "skipped cleanly" from "crashed the whole
    # adoption pass", which is why this test used to pass with its own guard deleted:
    # `_connect_facts_for(None, ...)` then raises AttributeError, `poll_once`'s blanket
    # handler swallows it, and the pid is still an untouched 4401. The caplog assertion is
    # the half that distinguishes them — and it also covers every OTHER row this tick,
    # which the crash would have skipped too.
    runner = _make_runner(runner_config)
    store = runner.persistence.state_store()
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    runner._instances["iid-gone"] = RemoteControlInstance(
        instance_id="iid-gone",
        project="deleted-project",
        label="deleted-project",
        status=InstanceStatus.STOPPED,
        bridge_pid=4401,
        bridge_proc_start=100.0,
    )
    store.save({"iid-gone": _row("deleted-project", pid=4402, proc_start=200.0)})
    monkeypatch.setattr(
        "clauster.runner.procutil.is_live_bridge", lambda pid, _s=None: pid == 4402
    )

    with caplog.at_level(logging.ERROR, logger="clauster.runner"):
        await runner.poll_once()

    inst = runner.get_instance("iid-gone")
    assert inst is not None
    assert inst.bridge_pid == 4401, "adopted pids for a project that no longer exists"
    assert not [r for r in caplog.records if "cross-process adoption failed" in r.getMessage()], (
        "the guard must SKIP this row, not raise out of the whole adoption pass"
    )


async def test_resync_takes_the_keeper_from_the_sidecar_not_the_row(runner_config, monkeypatch):
    # H-1, and it was pinned by nothing: swapping `_recover_keeper_pid` back for the row's
    # own `keeper_pid` left the whole suite green. A row's keeper_pid is written once at
    # spawn and never refreshed, so after a peer re-spawned the bridge it names a keeper
    # that is gone — or a pid since recycled onto an unrelated process, which `stop()`
    # would then kill a tree of. The sidecar is rewritten with the live pair, so it wins.
    import json

    runner = _make_runner(runner_config)
    _stub_connect(monkeypatch)
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    runner._instances["iid-a"] = RemoteControlInstance(
        instance_id="iid-a",
        project="alpha",
        label="alpha",
        resume_mode="pty",
        status=InstanceStatus.STOPPED,
        # Our own generation, now dead — a row whose pid is unknown is skipped by design.
        bridge_pid=7009,
        bridge_proc_start=299.0,
    )
    # The row still carries the keeper pid recorded at the ORIGINAL spawn.
    runner.persistence.state_store().save(
        {"iid-a": _row("alpha", pid=7010, proc_start=300.0, keeper_pid=9999)}
    )
    runner._log_dir.mkdir(parents=True, exist_ok=True)
    (runner._log_dir / "alpha-1700000000000-0.keeper.json").write_text(
        json.dumps(
            {
                "keeper_pid": 7011,
                "bridge_pid": 7010,
                "bridge_proc_start": 300.0,
                "state": "ready",
            }
        )
    )
    monkeypatch.setattr(
        "clauster.runner.procutil.is_live_bridge", lambda pid, _s=None: pid == 7010
    )

    await runner.poll_once()

    inst = runner.get_instance("iid-a")
    assert inst is not None
    assert inst.keeper_pid == 7011, "trusted the row's stale keeper_pid over the live sidecar"


async def test_resync_clears_a_stale_stop_intent_the_peer_already_undid(
    runner_config, monkeypatch
):
    # Also unpinned: deleting `current.intentional_stop = bool(saved.get(...))` left the
    # suite green. We stopped this instance, then a PEER process resumed it and wrote
    # intentional_stop=False with live pids. Adopting the pids while keeping our own stale
    # True leaves a card that reads "stopped on purpose" over a running bridge — so its
    # eventual death is not reported as a crash, and the operator is never told.
    runner = _make_runner(runner_config)
    _stub_connect(monkeypatch)
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    runner._instances["iid-a"] = RemoteControlInstance(
        instance_id="iid-a",
        project="alpha",
        label="alpha",
        resume_mode="standard",
        status=InstanceStatus.STOPPED,
        intentional_stop=True,
        bridge_pid=7019,  # our dead generation; the peer's is 7020
        bridge_proc_start=399.0,
    )
    runner.persistence.state_store().save(
        {
            "iid-a": _row(
                "alpha", pid=7020, proc_start=400.0, resume_mode="standard", intentional_stop=False
            )
        }
    )
    monkeypatch.setattr(
        "clauster.runner.procutil.is_live_bridge", lambda pid, _s=None: pid == 7020
    )

    await runner.poll_once()

    inst = runner.get_instance("iid-a")
    assert inst is not None
    assert inst.bridge_pid == 7020, "the peer's live pids were not adopted at all"
    assert inst.intentional_stop is False, "kept a stop intent the peer had already undone"


async def test_adoption_leaves_intent_alone_when_the_peer_recorded_no_stop(
    runner_config, monkeypatch
):
    # The other side of the same-generation arm: the row describes the very bridge we
    # already hold and records NO stop intent, so ours must be left exactly as it is.
    # Only the peer-stopped direction was covered, leaving the branch half-tested — and
    # an over-eager adoption here would mark a running bridge "stopped on purpose", which
    # suppresses crash reporting for it.
    runner = _make_runner(runner_config)
    _stub_connect(monkeypatch)
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    runner._instances["iid-a"] = RemoteControlInstance(
        instance_id="iid-a",
        project="alpha",
        label="alpha",
        status=InstanceStatus.RUNNING,
        bridge_pid=7030,
        bridge_proc_start=500.0,
    )
    # Same (pid, proc_start) as the instance -> the same-generation arm, not a re-sync.
    runner.persistence.state_store().save({"iid-a": _row("alpha", pid=7030, proc_start=500.0)})

    await runner.poll_once()

    inst = runner.get_instance("iid-a")
    assert inst is not None
    assert inst.intentional_stop is False, "invented a stop intent the row never recorded"
    assert inst.status is InstanceStatus.RUNNING


async def test_resync_aborts_when_the_row_is_forgotten_while_recovering_facts(
    runner_config, monkeypatch
):
    # Connect-fact recovery is a thread hop, and `forget()` is not gated by the spawn lock.
    # A row dropped during that window must not be written back onto the instance.
    runner = _make_runner(runner_config)
    store = runner.persistence.state_store()
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    runner._instances["iid-a"] = RemoteControlInstance(
        instance_id="iid-a",
        project="alpha",
        label="alpha",
        status=InstanceStatus.STOPPED,
        bridge_pid=4401,
        bridge_proc_start=100.0,
    )
    store.save({"iid-a": _row("alpha", pid=4402, proc_start=200.0)})
    monkeypatch.setattr(
        "clauster.runner.procutil.is_live_bridge", lambda pid, _s=None: pid == 4402
    )

    def _forget_mid_flight(self, proj, mode, pid, start):
        self._persisted.pop("iid-a", None)  # another process's `clauster forget`
        return {"url": "https://claude.ai/code?environment=env_STUB"}

    monkeypatch.setattr(SessionRunner, "_connect_facts_for", _forget_mid_flight)

    await runner.poll_once()

    inst = runner.get_instance("iid-a")
    assert inst is not None
    assert inst.bridge_pid == 4401, "resurrected pids for a row another process forgot"


async def test_resync_does_not_revert_a_stop_that_completed_during_the_probe(
    runner_config, monkeypatch
):
    # The lock alone is not enough. It excludes a CONCURRENT stop(), but a stop() that
    # COMPLETED while the poll was inside the liveness probe holds no lock by the time the
    # resync looks — and it mutates `status` and `intentional_stop` while deliberately
    # leaving `bridge_pid` in place, so a pid-only generation check passes and the
    # operator's Stop silently comes back RUNNING (and does not self-heal: the next
    # _persist writes intentional_stop=False over the row).
    runner = _make_runner(runner_config)
    store = runner.persistence.state_store()
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    runner._instances["iid-a"] = RemoteControlInstance(
        instance_id="iid-a",
        project="alpha",
        label="alpha",
        status=InstanceStatus.RUNNING,
        bridge_pid=4401,
        bridge_proc_start=100.0,
    )
    store.save({"iid-a": _row("alpha", pid=4402, proc_start=200.0)})
    _stub_connect(monkeypatch)

    def _is_live(pid, _s=None):
        inst = runner._instances["iid-a"]
        if inst.status is InstanceStatus.RUNNING:
            # stop() lands and finishes while we are in the probe.
            inst.status = InstanceStatus.STOPPED
            inst.intentional_stop = True
        return pid == 4402

    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", _is_live)

    await runner.poll_once()

    inst = runner.get_instance("iid-a")
    assert inst is not None
    assert inst.status is InstanceStatus.STOPPED, "reverted a Stop the operator completed"
    assert inst.intentional_stop is True, "cleared the recorded stop intent"
    assert inst.bridge_pid == 4401, "re-pointed a stopped card at the peer's live bridge"


async def test_resync_refuses_an_instance_that_is_no_longer_the_registered_one(runner_config):
    # Pins the post-lock identity guard directly. Between the poll loop reading the instance
    # and this call taking the lock, a lock holder can have swapped the registry entry — and
    # `forget()` can have dropped the row. Mutating the stale object would edit something no
    # reader can see; writing it back would resurrect a forgotten row.
    runner = _make_runner(runner_config)
    registered = RemoteControlInstance(
        instance_id="iid-a",
        project="alpha",
        label="alpha",
        status=InstanceStatus.RUNNING,
        bridge_pid=5555,
        bridge_proc_start=400.0,
    )
    runner._instances["iid-a"] = registered
    stale = RemoteControlInstance(
        instance_id="iid-a",
        project="alpha",
        label="alpha",
        status=InstanceStatus.STOPPED,
        bridge_pid=4401,
        bridge_proc_start=100.0,
    )

    await runner._resync_pids_from_row(
        "iid-a",
        stale,
        {"project_name": "alpha"},
        (4402, 200.0),
        (4401, 100.0, InstanceStatus.STOPPED, False),
        runner._discovered(),
    )

    assert registered.bridge_pid == 5555, "mutated the live registry entry"
    assert stale.bridge_pid == 4401, "mutated a stale object the registry no longer holds"


async def test_resync_skips_a_project_whose_spawn_lock_is_held(runner_config, monkeypatch):
    # MF-2 / H-2. The re-sync is the only bridge_pid mutator off the spawn lock, so it could
    # land mid-`resume()` and overwrite a freshly-spawned live pid (orphaning a bridge that
    # nothing can stop), or interleave with `stop()` and persist another process's LIVE pid
    # as intentionally stopped. Holding the lock must make it defer to the next tick.
    runner = _make_runner(runner_config)
    store = runner.persistence.state_store()
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    runner._instances["iid-a"] = RemoteControlInstance(
        instance_id="iid-a",
        project="alpha",
        label="alpha",
        status=InstanceStatus.STOPPED,
        bridge_pid=4401,
        bridge_proc_start=100.0,
    )
    store.save({"iid-a": _row("alpha", pid=4402, proc_start=200.0)})
    monkeypatch.setattr(
        "clauster.runner.procutil.is_live_bridge", lambda pid, _s=None: pid == 4402
    )
    _stub_connect(monkeypatch)

    async with runner._spawn_lock_for("alpha"):  # stand in for an in-flight resume()/stop()
        await runner.poll_once()

    inst = runner.get_instance("iid-a")
    assert inst is not None
    assert inst.bridge_pid == 4401, "must not mutate pids while the project's lock is held"
    assert inst.status is InstanceStatus.STOPPED


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
    (runner._log_dir / "alpha-900-1.keeper.json").write_text(
        json.dumps({"bridge_pid": 8401, "state": "starting", "connect_url": "u-notready"})
    )
    (runner._log_dir / "alpha-800-1.keeper.json").write_text(
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


# ---------------------------------------------------------------------------
# Bug 1150 — stop reports success while stopping nothing
# ---------------------------------------------------------------------------


async def test_resolve_bridge_id_refuses_ambiguous_project_name(runner_config, monkeypatch):
    # #1150. A project with a live bridge (iid-live) and a later-stopped row (iid-dead).
    # resolve_bridge_id used to silently return iid-dead (last-registered wins), so
    # `clauster stop alpha` reported success without touching the live bridge.
    # The fix: two matches for the same project name → (None, candidates), identical to
    # the ambiguous id-prefix behaviour (#1099). The caller then surfaces a 409 / error.
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

    # The bare project name must now refuse, not guess.
    assert runner.resolve_bridge_id("alpha") is None
    candidates = runner.bridge_id_candidates("alpha")
    assert set(candidates) == {"iid-live", "iid-dead"}, f"unexpected candidates: {candidates}"


async def test_resolve_bridge_id_resolves_unambiguous_project_name(runner_config, monkeypatch):
    # Unambiguous case: exactly one instance for a project — still resolves normally.
    runner = _make_runner(runner_config)
    _stub_connect(monkeypatch)
    runner.persistence.state_store().save({"iid-only": _row("alpha", pid=5001)})
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda *a, **k: True)

    await runner.rediscover(persist=False)

    assert runner.resolve_bridge_id("alpha") == "iid-only"
    assert runner.bridge_id_candidates("alpha") == []


# ---------------------------------------------------------------------------
# Bug 1106 — an adopted bridge with no connect evidence is pinned at STARTING
# ---------------------------------------------------------------------------


def _adopted_starting(runner: SessionRunner, *, pid: int = 7001, project: str = "alpha"):
    """Register the shape #1106 describes: adopted, alive, STARTING, no connect facts."""
    inst = RemoteControlInstance(
        instance_id="iid-adopted",
        project=project,
        label=project,
        status=InstanceStatus.STARTING,
        bridge_pid=pid,
        bridge_proc_start=100.0,
    )
    runner._instances[inst.instance_id] = inst
    return inst


async def test_adopted_starting_bridge_promotes_once_connect_evidence_lands(
    runner_config, monkeypatch
):
    # THE #1106 regression test, as the issue's four-tick probe: an instance adopted from
    # another process's row before its pointer/ready sidecar existed. `_reconcile_status`
    # only demotes, the re-sync is one-shot, and every other promotion path is a self-spawn
    # path — so before the fix this stayed `status=starting url=None` on every later tick.
    runner = _make_runner(runner_config)
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    inst = _adopted_starting(runner)

    evidence: dict = {}
    monkeypatch.setattr(SessionRunner, "_connect_facts_for", lambda *a, **k: dict(evidence))

    await runner.poll_once()  # tick 1: no evidence yet
    assert inst.status is InstanceStatus.STARTING
    assert inst.url is None

    evidence.update(  # tick 2: the peer's bridge finished registering
        {
            "url": "https://claude.ai/code?environment=env_LATE",
            "environment_id": "env_LATE",
            "starter_session_id": "session_LATE",
        }
    )
    await runner.poll_once()

    assert inst.status is InstanceStatus.RUNNING, "pinned at STARTING with evidence available"
    assert inst.url == "https://claude.ai/code?environment=env_LATE"
    assert inst.environment_id == "env_LATE"
    assert inst.starter_session_id == "session_LATE"


async def test_alive_but_unregistered_bridge_is_not_promoted(runner_config, monkeypatch):
    # Fail closed, visibly: liveness is not usability. Without connect evidence the honest
    # state is STARTING — promoting on a live process alone is exactly what `_reconcile_status`
    # refuses to do, and it reported uncontrollable bridges as RUNNING.
    runner = _make_runner(runner_config)
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    monkeypatch.setattr(SessionRunner, "_connect_facts_for", lambda *a, **k: {})
    inst = _adopted_starting(runner)

    await runner.poll_once()

    assert inst.status is InstanceStatus.STARTING
    assert inst.url is None


async def test_dead_starting_bridge_is_reconciled_not_promoted(runner_config, monkeypatch):
    # A STARTING row whose process is gone must take the demotion, never the promotion —
    # even if a stale pointer/sidecar would still answer with connect facts.
    runner = _make_runner(runner_config)
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    _stub_connect(monkeypatch)
    inst = _adopted_starting(runner)

    await runner.poll_once()

    assert inst.status is InstanceStatus.CRASHED
    assert inst.url is None


async def test_promotion_leaves_a_bridge_with_its_own_startup_watch_alone(
    runner_config, monkeypatch
):
    # A self-spawned bridge already has an owner for its readiness: `_watch_startup`, which
    # also runs `_post_spawn_enrich` and can decide ERROR on the grace deadline. The poll must
    # not race it into RUNNING behind its back.
    runner = _make_runner(runner_config)
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    _stub_connect(monkeypatch)
    inst = _adopted_starting(runner)

    async def _never() -> None:
        await asyncio.Event().wait()

    watch = asyncio.create_task(_never())
    runner._startup_watches[inst.instance_id] = watch
    try:
        await runner.poll_once()
    finally:
        watch.cancel()

    assert inst.status is InstanceStatus.STARTING, "stole a promotion from the startup-watch"


async def test_promotion_skips_a_row_whose_generation_changed_mid_probe(
    runner_config, monkeypatch
):
    # The evidence read happens off-loop. If a resume() republishes the instance onto a NEW
    # process generation during that hop, the facts in hand describe the OLD one — publishing
    # them would hand the operator a link into an environment that is already gone.
    runner = _make_runner(runner_config)
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    inst = _adopted_starting(runner)

    def _resume_lands(self, proj, mode, pid, start):
        inst.bridge_pid, inst.bridge_proc_start = 9999, 500.0
        return {"url": "https://claude.ai/code?environment=env_OLD"}

    monkeypatch.setattr(SessionRunner, "_connect_facts_for", _resume_lands)

    await runner.poll_once()

    assert inst.status is InstanceStatus.STARTING
    assert inst.url is None, "published the previous generation's connect link"


async def test_promotion_skips_a_bridge_that_died_during_the_evidence_read(
    runner_config, monkeypatch
):
    # The review catch: the post-hop guard proves the SAME GENERATION, not that it is
    # still alive. A bridge exiting after the pre-hop liveness filter but before the
    # evidence read completes left stale-but-matching pid/start fields — promoting on
    # them emits a false `ready` for a dead process, corrected only a full tick later.
    # Liveness is now re-probed after the evidence read inside the same hop.
    runner = _make_runner(runner_config)
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    state = {"read": False}

    def _dies_during_read(self, proj, mode, pid, start):
        state["read"] = True
        return {"url": "https://claude.ai/code?environment=env_DEAD"}

    monkeypatch.setattr(
        "clauster.runner.procutil.is_live_bridge",
        lambda *a, **k: not state["read"],  # alive until the read happens, then gone
    )
    monkeypatch.setattr(SessionRunner, "_connect_facts_for", _dies_during_read)
    inst = _adopted_starting(runner)

    await runner.poll_once()

    assert inst.status is InstanceStatus.STARTING, "promoted a bridge that died mid-read"
    assert inst.url is None


async def test_promotion_skips_an_undiscovered_project(runner_config, monkeypatch, caplog):
    # No Project means nothing to read the pointer/sidecar from, so the row is left alone
    # rather than promoted half-populated — and the skip must not raise out of the tick.
    runner = _make_runner(runner_config)
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    _stub_connect(monkeypatch)
    inst = _adopted_starting(runner, project="deleted-project")

    with caplog.at_level(logging.ERROR, logger="clauster.runner"):
        await runner.poll_once()

    assert inst.status is InstanceStatus.STARTING
    assert not caplog.records, f"the guard must SKIP this row, not raise: {caplog.records}"


async def test_observation_only_poll_promotes_but_stays_silent(runner_config, monkeypatch):
    # `side_effects=False` is write-free, not read-only (see poll_once): a headless reader must
    # SEE the real state, so the promotion still happens — it just doesn't announce it, the
    # same rule the crash arm follows so one transition can't notify twice.
    runner = _make_runner(runner_config)
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    _stub_connect(monkeypatch)
    inst = _adopted_starting(runner)
    emitted: list[str] = []
    monkeypatch.setattr(SessionRunner, "_emit_lifecycle", lambda self, e, i: emitted.append(e))

    await runner.poll_once(side_effects=False)

    assert inst.status is InstanceStatus.RUNNING
    assert emitted == [], "an observation-only poll must not announce the transition"


async def test_promotion_announces_the_transition_once(runner_config, monkeypatch):
    # The `ready` event is what the self-spawn promotion paths emit, and it must fire on the
    # transition only — a second tick over an already-RUNNING bridge announces nothing.
    runner = _make_runner(runner_config)
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    _stub_connect(monkeypatch)
    inst = _adopted_starting(runner)
    emitted: list[str] = []
    monkeypatch.setattr(SessionRunner, "_emit_lifecycle", lambda self, e, i: emitted.append(e))

    await runner.poll_once()
    await runner.poll_once()

    assert inst.status is InstanceStatus.RUNNING
    assert emitted == ["ready"]
