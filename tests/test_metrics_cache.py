"""Server-side metrics snapshot cache (#354): the runner samples off the request path."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from clauster.models import Attribution, InstanceStatus, RemoteControlInstance, WorkingSession
from clauster.runner import SessionRunner
from conftest import _raise_cancelled


def _runner(runner_config) -> SessionRunner:
    config, claude_json = runner_config
    return SessionRunner(config, claude_json=claude_json)


def _running(runner, project="alpha", *, pid=None, start=None):
    inst = RemoteControlInstance(project=project, label=project)
    inst.status = InstanceStatus.RUNNING
    inst.bridge_pid = os.getpid() if pid is None else pid
    inst.bridge_proc_start = start
    # Registry keyed by instance_id (#777) — several instances may share a project.
    runner._instances[inst.instance_id] = inst
    return inst


@pytest.fixture
def live_pid():
    """Yield the PID of a real, childless subprocess to sample.

    The real-sample tests must NOT sample ``os.getpid()`` — the pytest-xdist worker.
    ``metrics.sample_tree`` walks ``children(recursive=True)`` and then queries
    cpu/memory/io on every descendant; the worker's tree includes the *leaked, dying*
    subprocesses of sibling tests on the same worker, and querying a psutil handle to a
    dying process on Windows faulted hard enough to crash the whole xdist worker (an
    uncatchable native crash, not a Python exception the ``_GONE`` guards can swallow).
    The odds rose whenever a PR added tests (more sibling churn), which is how this
    surfaced. A dedicated childless subprocess bounds the walk to one stable, alive
    process, so sampling is deterministic and can't fault on someone else's teardown.
    Mirrors the spawn/teardown pattern in ``test_procutil.py``.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield proc.pid
    finally:
        proc.kill()  # a sleeping child dies at once → a single, always-covered path
        proc.wait()


async def test_refresh_samples_live_bridge_into_cache(runner_config, live_pid):
    runner = _runner(runner_config)
    _running(runner, pid=live_pid)  # a real, childless subprocess → a live sample
    await runner._refresh_metrics_cache()
    sample = runner.metrics_snapshot("alpha")
    assert sample is not None and "cpu_percent" in sample and sample["procs"] >= 1
    assert runner.metrics_snapshots()["alpha"] is not None


async def test_refresh_excludes_non_running(runner_config):
    runner = _runner(runner_config)
    inst = _running(runner)
    inst.status = InstanceStatus.STOPPED
    await runner._refresh_metrics_cache()
    assert runner.metrics_snapshot("alpha") is None


async def test_refresh_replaces_wholesale_dropping_stale(runner_config, live_pid):
    runner = _runner(runner_config)
    runner._metrics_cache = {"ghost": {"cpu_percent": 9.0}}  # a stale, now-gone bridge
    _running(runner, pid=live_pid)
    await runner._refresh_metrics_cache()
    assert "ghost" not in runner.metrics_snapshots()  # stale entry dropped
    assert runner.metrics_snapshot("alpha") is not None


async def test_refresh_drops_bridge_on_pid_reuse(runner_config):
    runner = _runner(runner_config)
    _running(runner, start=1.0)  # recorded start ≠ this pid's real create-time → reuse
    await runner._refresh_metrics_cache()
    assert runner.metrics_snapshot("alpha") is None


async def test_refresh_samples_when_pid_create_time_matches(runner_config, live_pid):
    # start set AND matching the live pid's create-time → the guard passes, bridge sampled.
    from clauster import procutil

    runner = _runner(runner_config)
    _running(runner, pid=live_pid, start=procutil.proc_create_time(live_pid))
    await runner._refresh_metrics_cache()
    assert runner.metrics_snapshot("alpha") is not None


async def test_refresh_pid_reuse_guard_uses_tight_tolerance(runner_config):
    # Item-6 (#408): bridge_proc_start is OUR OWN proc_create_time() of this pid, so a
    # live match is near-exact. A recorded start off by 0.5s (inside the OLD loose 2.0s
    # window, outside the tight _EXACT_PROC_START_TOLERANCE of 0.05s) must now be
    # rejected as a recycled pid — the loose window let such a mismatch through.
    from clauster import procutil

    assert procutil._EXACT_PROC_START_TOLERANCE < 0.5 < 2.0  # the window the fix closed
    runner = _runner(runner_config)
    off = procutil.proc_create_time(os.getpid()) + 0.5
    _running(runner, start=off)
    await runner._refresh_metrics_cache()
    assert runner.metrics_snapshot("alpha") is None  # tight guard drops it


async def test_refresh_drops_bridge_on_dead_pid(runner_config):
    runner = _runner(runner_config)
    _running(runner, pid=2_147_483_646)  # not a live pid → sample None
    await runner._refresh_metrics_cache()
    assert runner.metrics_snapshot("alpha") is None


async def test_refresh_drops_bridge_on_sampling_error(runner_config, monkeypatch):
    runner = _runner(runner_config)
    _running(runner)

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("clauster.runner.metrics.sample_tree", _boom)
    await runner._refresh_metrics_cache()  # must not raise
    assert runner.metrics_snapshot("alpha") is None


async def test_refresh_drops_bridge_on_cancelled_sample(runner_config, monkeypatch):
    # A per-task CancelledError is stored by gather(return_exceptions=True) as a
    # BaseException (not an Exception) — it must be dropped, never mis-stored as a
    # sample. Guards the isinstance(..., BaseException) check (#407 review).
    runner = _runner(runner_config)
    _running(runner)

    def _cancel(*a, **k):
        raise asyncio.CancelledError

    monkeypatch.setattr("clauster.runner.metrics.sample_tree", _cancel)
    await runner._refresh_metrics_cache()  # must not raise; the bridge is dropped
    assert runner.metrics_snapshot("alpha") is None


async def test_refresh_samples_bridges_concurrently(runner_config, monkeypatch):
    # The N per-bridge samples run concurrently (#407). Asserted structurally, not by
    # wall-clock: a shared counter records how many samplers are in-flight at once. The
    # serial loop would never exceed 1; the concurrent gather drives several at once. A
    # barrier blocks each sampler until all N have entered, so the peak is observable
    # regardless of thread-pool width or scheduler timing.
    runner = _runner(runner_config)
    n = 4
    for i in range(n):
        _running(runner, project=f"bridge{i}")

    barrier = threading.Barrier(n, timeout=5)
    lock = threading.Lock()
    in_flight = 0
    peak = 0

    def _tracked(pid, **k):
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        barrier.wait()  # hold until all N samplers have arrived → forces overlap
        with lock:
            in_flight -= 1
        return {"cpu_percent": 1.0, "procs": 1}

    monkeypatch.setattr("clauster.runner.metrics.sample_tree", _tracked)
    await runner._refresh_metrics_cache()

    assert len(runner.metrics_snapshots()) == n  # every bridge sampled
    assert peak == n  # all N ran at once — a serial loop would peak at 1


async def test_refresh_isolates_one_failing_bridge_from_the_rest(runner_config, monkeypatch):
    # One bridge's sampler raising must drop only that bridge, never abort the others
    # (#407 preserves the per-bridge error isolation of the old serial loop). Both bridges
    # use the _running default start=None so the create-time guard is skipped and the
    # sentinel pid actually reaches sample_tree — i.e. "bad" is dropped by the EXCEPTION
    # path, not by a guard short-circuit.
    runner = _runner(runner_config)
    _running(runner, project="good")
    bad = _running(runner, project="bad")

    def _selective(pid, **k):
        # The "bad" bridge is identified by a sentinel pid set below.
        if pid == 999_999:
            raise RuntimeError("boom")
        return {"cpu_percent": 1.0, "procs": 1}

    bad.bridge_pid = 999_999
    monkeypatch.setattr("clauster.runner.metrics.sample_tree", _selective)
    await runner._refresh_metrics_cache()  # must not raise
    assert runner.metrics_snapshot("good") is not None
    assert runner.metrics_snapshot("bad") is None


async def _noop():
    return None


async def test_metrics_task_started_when_enabled_and_cancelled_on_shutdown(
    runner_config, monkeypatch
):
    runner = _runner(runner_config)
    assert runner._config.metrics.enabled  # default on
    monkeypatch.setattr(runner, "rediscover", _noop)  # skip discovery for a focused test
    await runner.start_poll_loop()
    try:
        assert runner._metrics_task is not None
    finally:
        await runner.shutdown()
    assert runner._metrics_task is None  # cancelled + cleared


async def test_metrics_task_not_started_when_disabled(runner_config, monkeypatch):
    config, claude_json = runner_config
    config.metrics.enabled = False
    runner = SessionRunner(config, claude_json=claude_json)
    monkeypatch.setattr(runner, "rediscover", _noop)
    await runner.start_poll_loop()
    try:
        assert runner._metrics_task is None
    finally:
        await runner.shutdown()


async def test_refresh_forever_continues_after_unexpected_error(runner_config, monkeypatch):
    # An unexpected error from _refresh_metrics_cache is caught by the loop and never
    # propagated; the loop reaches its sleep (which we make exit the test).
    runner = _runner(runner_config)

    async def _boom():
        raise RuntimeError("unexpected")

    # #1157: `_metrics_refresh_forever` lives in PollLoop and calls its OWN
    # `_refresh_metrics_cache` + `asyncio.sleep`, so patch the collaborator's method and the
    # poll_loop module's asyncio.
    monkeypatch.setattr(runner._poll_loop, "_refresh_metrics_cache", _boom)
    monkeypatch.setattr("clauster.poll_loop.asyncio.sleep", _raise_cancelled)
    with pytest.raises(asyncio.CancelledError):  # only the sleep's cancel escapes
        await runner._metrics_refresh_forever()


async def test_refresh_forever_propagates_cancel_from_refresh(runner_config, monkeypatch):
    # A CancelledError from the refresh itself is re-raised (not swallowed by the loop),
    # so task cancellation stops the loop promptly.
    runner = _runner(runner_config)

    async def _cancel():
        raise asyncio.CancelledError

    # #1157: patch PollLoop's own `_refresh_metrics_cache` — the delegator's target — so the
    # cancel actually propagates out of the loop.
    monkeypatch.setattr(runner._poll_loop, "_refresh_metrics_cache", _cancel)
    with pytest.raises(asyncio.CancelledError):
        await runner._metrics_refresh_forever()


def test_warn_if_refresh_slow(runner_config, caplog):
    # A refresh slower than poll_seconds warns; a fast one is silent (no spam).
    runner = _runner(runner_config)
    poll = runner._config.metrics.poll_seconds
    with caplog.at_level(logging.WARNING, logger="clauster.runner"):
        runner._warn_if_refresh_slow(poll + 100)
    assert any("metrics refresh took" in r.message for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="clauster.runner"):
        runner._warn_if_refresh_slow(0.0)
    assert not caplog.records


# ----- per-project aggregation over the instance_id-keyed cache (#778) ----------


async def test_snapshots_aggregate_bridges_of_one_project(runner_config, monkeypatch):
    """N bridges of one project fold into a single per-project figure (#778).

    The cache holds one sample per instance; the public readers sum procs/cpu/rss,
    sum a disk rate when any bridge reports it, and count the covered ``bridges`` —
    a project key would have kept only whichever bridge sampled last.
    """
    runner = _runner(runner_config)
    _running(runner, project="alpha", pid=101)
    _running(runner, project="alpha", pid=102)
    _running(runner, project="beta", pid=103)

    samples = {
        101: {
            "procs": 2,
            "cpu_percent": 1.5,
            "cpu_normalized": False,
            "rss_bytes": 100,
            "disk_read_bps": 10,
            "disk_write_bps": None,
        },
        102: {
            "procs": 3,
            "cpu_percent": 2.3,
            "cpu_normalized": False,
            "rss_bytes": 200,
            "disk_read_bps": None,
            "disk_write_bps": 5,
        },
        103: {
            "procs": 1,
            "cpu_percent": 9.0,
            "cpu_normalized": False,
            "rss_bytes": 50,
            "disk_read_bps": None,
            "disk_write_bps": None,
        },
    }
    monkeypatch.setattr("clauster.runner.metrics.sample_tree", lambda pid, **k: dict(samples[pid]))
    await runner._refresh_metrics_cache()

    snaps = runner.metrics_snapshots()
    alpha = snaps["alpha"]
    assert alpha["bridges"] == 2
    assert alpha["procs"] == 5
    assert alpha["cpu_percent"] == 3.8
    assert alpha["rss_bytes"] == 300
    assert alpha["disk_read_bps"] == 10  # one bridge reported → None treated as 0
    assert alpha["disk_write_bps"] == 5
    # The other project stays separate, uncontaminated by alpha's fold.
    assert snaps["beta"]["bridges"] == 1 and snaps["beta"]["rss_bytes"] == 50
    assert runner.metrics_snapshot("alpha") == alpha  # single read agrees with batch


async def test_snapshots_drop_sample_for_vanished_instance(runner_config):
    """A cached sample whose instance left the registry between refreshes is dropped (#778)."""
    runner = _runner(runner_config)
    runner._metrics_cache = {"gone-iid": {"procs": 1, "cpu_percent": 1.0, "rss_bytes": 10}}
    assert runner.metrics_snapshots() == {}
    assert runner.metrics_snapshots_by_instance() == {}
    assert runner.metrics_snapshot("alpha") is None


# ----- per-instance reader, un-folded (#1090) -----------------------------------


async def test_snapshots_by_instance_keep_bridges_of_one_project_apart(runner_config, monkeypatch):
    """Each bridge of one project keeps its OWN figure, un-folded (#1090).

    The project fold credits a single row with every co-located bridge's CPU/RAM; the
    per-instance reader is what a dashboard row must use to report only itself.
    """
    runner = _runner(runner_config)
    server = _running(runner, project="alpha", pid=201)
    interactive = _running(runner, project="alpha", pid=202)

    disk = {"disk_read_bps": None, "disk_write_bps": None}
    samples = {
        201: {"procs": 1, "cpu_percent": 1.0, "rss_bytes": 100, **disk},
        202: {"procs": 3, "cpu_percent": 8.0, "rss_bytes": 900, **disk},
    }
    monkeypatch.setattr("clauster.runner.metrics.sample_tree", lambda pid, **k: dict(samples[pid]))
    await runner._refresh_metrics_cache()

    by_iid = runner.metrics_snapshots_by_instance()
    # The runner adds the (here empty) per-session breakdown to every sample.
    assert by_iid[server.instance_id] == {**samples[201], "sessions": {}}
    assert by_iid[interactive.instance_id] == {**samples[202], "sessions": {}}
    # ... while the project reader still sums them for the callers that want a total.
    assert runner.metrics_snapshot("alpha")["rss_bytes"] == 1000


async def test_snapshots_by_instance_returns_copies(runner_config):
    """Mutating a returned sample must not corrupt the shared cache (#1090)."""
    runner = _runner(runner_config)
    inst = _running(runner, project="alpha")
    runner._metrics_cache[inst.instance_id] = {"procs": 1, "cpu_percent": 1.0, "rss_bytes": 10}
    runner.metrics_snapshots_by_instance()[inst.instance_id]["rss_bytes"] = 999
    assert runner._metrics_cache[inst.instance_id]["rss_bytes"] == 10


# ----- per-session breakdown (backlog TASK-342) ---------------------------------

_T0 = 1_000_000.0  # a session start, in epoch seconds


def _session(pid, uuid, parent, attribution=Attribution.TRACKED, started=_T0):
    s = WorkingSession(
        pid=pid,
        cwd=Path("/p"),
        kind="interactive",
        started_at=int(started * 1000),
        local_uuid=uuid,
    )
    s.parent_instance = parent
    s.attribution = attribution
    return s


def _figure(pid, *, start=(_T0, 500), rss=200):
    return {"pid": pid, "procs": 2, "cpu_percent": 3.0, "rss_bytes": rss, "start": start}


async def test_session_pids_come_from_this_bridges_tracked_sessions(runner_config):
    """Only this bridge's TRACKED sessions, each with one pid and one uuid, are asked for."""
    runner = _runner(runner_config)
    inst = _running(runner, project="alpha", pid=100)
    other = _running(runner, project="alpha", pid=200)
    runner._sessions = [
        _session(101, "u-a", inst.instance_id),
        _session(102, "u-b", inst.instance_id),
        _session(201, "u-other", other.instance_id),  # another bridge's session
        _session(103, "u-ext", None, Attribution.EXTERNAL),
        _session(104, "u-ext2", inst.instance_id, Attribution.HOSTED),
        _session(105, "", inst.instance_id),  # no uuid → no key to report under
        _session(106, "u-dup", inst.instance_id),  # one uuid on two pids → ambiguous
        _session(107, "u-dup", inst.instance_id),
        _session(108, "u-c", inst.instance_id),  # one pid named by two sessions
        _session(108, "u-d", inst.instance_id),
    ]
    mine = runner._session_pids_for(inst)
    assert {p: s.local_uuid for p, s in mine.items()} == {101: "u-a", 102: "u-b"}
    theirs = runner._session_pids_for(other)
    assert {p: s.local_uuid for p, s in theirs.items()} == {201: "u-other"}


async def test_session_pids_empty_for_a_pty_bridge(runner_config):
    """A pty bridge is one session; its bridge figure already is that session."""
    runner = _runner(runner_config)
    inst = _running(runner, project="alpha", pid=100)
    inst.resume_mode = "pty"
    runner._sessions = [_session(101, "u-a", inst.instance_id)]
    assert runner._session_pids_for(inst) == {}


async def test_refresh_hands_the_sampler_this_bridges_session_pids(runner_config, monkeypatch):
    """The sampler gets the session map, and the breakdown lands in the per-instance cache."""
    runner = _runner(runner_config)
    inst = _running(runner, project="alpha", pid=100)
    runner._sessions = [_session(101, "u-a", inst.instance_id)]
    seen = {}

    def _sample(pid, **kw):
        seen[pid] = kw["sessions"]
        return {
            "procs": 3,
            "cpu_percent": 4.0,
            "rss_bytes": 300,
            "sessions": {"u-a": _figure(101)},
        }

    monkeypatch.setattr("clauster.runner.metrics.sample_tree", _sample)
    await runner._refresh_metrics_cache()
    assert seen == {100: {101: "u-a"}}
    by_iid = runner.metrics_snapshots_by_instance()
    entry = by_iid[inst.instance_id]["sessions"]["u-a"]
    assert entry["rss_bytes"] == 200
    assert "start" not in entry  # the identity is the runner's, not the API's
    # Copied all the way down: a caller cannot reach into the cache through the breakdown.
    entry["rss_bytes"] = 1
    assert runner._metrics_cache[inst.instance_id]["sessions"]["u-a"]["rss_bytes"] == 200
    # The project fold describes the project, so one bridge's sessions are not on it.
    assert "sessions" not in runner.metrics_snapshot("alpha")


async def test_session_figure_dropped_once_its_pid_names_another_process(runner_config):
    """A pid reused inside the bridge tree never carries the old session's figure."""
    runner = _runner(runner_config)
    inst = _running(runner, project="alpha", pid=100)
    tracked = {101: _session(101, "u-a", inst.instance_id)}
    vet = runner._vet_session_figures
    assert "u-a" in vet(inst.instance_id, tracked, {"u-a": _figure(101)})
    # Same process (same ticks) on the next tick: still shown.
    assert "u-a" in vet(inst.instance_id, tracked, {"u-a": _figure(101)})
    # The pid now names a process that started at another tick: dropped, and it stays
    # dropped, because the pin is not replaced by the newcomer.
    reused = {"u-a": _figure(101, start=(_T0 + 9, 900))}
    assert vet(inst.instance_id, tracked, reused) == {}
    assert vet(inst.instance_id, tracked, reused) == {}


async def test_session_figure_needs_an_identity(runner_config):
    """A figure whose process start could not be read is left out, not shown unpinned."""
    runner = _runner(runner_config)
    inst = _running(runner, project="alpha", pid=100)
    tracked = {101: _session(101, "u-a", inst.instance_id)}
    figure = {"u-a": _figure(101, start=(None, None))}
    assert runner._vet_session_figures(inst.instance_id, tracked, figure) == {}
    # Nothing was pinned, so the next complete read is measured afresh and shown.
    assert "u-a" in runner._vet_session_figures(inst.instance_id, tracked, {"u-a": _figure(101)})


async def test_session_figure_must_match_the_tracked_session(runner_config):
    """A figure for a uuid/pid pair the tracked list does not hold is left out."""
    runner = _runner(runner_config)
    inst = _running(runner, project="alpha", pid=100)
    tracked = {101: _session(101, "u-a", inst.instance_id)}
    vet = runner._vet_session_figures
    assert vet(inst.instance_id, tracked, {"u-b": _figure(101)}) == {}
    assert vet(inst.instance_id, tracked, {"u-a": _figure(999)}) == {}


async def test_first_figure_checked_against_started_at_where_epoch_is_fixed(
    runner_config, monkeypatch
):
    """On macOS/Windows a first figure must have started near the CLI's `startedAt`."""
    monkeypatch.setattr("clauster.runner.procutil.start_time_is_drift_prone", lambda: False)
    runner = _runner(runner_config)
    inst = _running(runner, project="alpha", pid=100)
    tracked = {101: _session(101, "u-a", inst.instance_id, started=_T0)}
    vet = runner._vet_session_figures
    # A process that started an hour after the session began is a reused pid.
    late = {"u-a": _figure(101, start=(_T0 + 3600, None))}
    assert vet(inst.instance_id, tracked, late) == {}
    # One second off, as measured on a live bridge: the session's own process.
    near = {"u-a": _figure(101, start=(_T0 + 1, None))}
    assert "u-a" in vet(inst.instance_id, tracked, near)
    assert "u-a" in vet(inst.instance_id, tracked, near)  # pinned on the epoch alone
    moved = {"u-a": _figure(101, start=(_T0 + 2, None))}
    assert vet(inst.instance_id, tracked, moved) == {}


async def test_started_at_check_is_coarse_where_the_epoch_drifts(
    runner_config, monkeypatch, caplog
):
    """On Linux the epoch moves with the wall clock (#1399): the ticks decide identity.

    The first-figure startedAt check there uses the coarse hour bound, so a corrected clock
    does not refuse a real session, while a pid reused after an hours-long session does.
    """
    monkeypatch.setattr("clauster.runner.procutil.start_time_is_drift_prone", lambda: True)
    runner = _runner(runner_config)
    inst = _running(runner, project="alpha", pid=100)
    tracked = {
        101: _session(101, "u-a", inst.instance_id, started=_T0),
        102: _session(102, "u-b", inst.instance_id, started=_T0),
        103: _session(103, "u-c", inst.instance_id, started=_T0),
    }
    vet = runner._vet_session_figures
    # An epoch 90s from startedAt (a corrected clock) does not block the first figure,
    # and a later epoch shift with the same ticks keeps it.
    assert "u-a" in vet(inst.instance_id, tracked, {"u-a": _figure(101, start=(_T0 + 90, 7))})
    assert "u-a" in vet(inst.instance_id, tracked, {"u-a": _figure(101, start=(_T0 + 95, 7))})
    # The pinned epoch exactly, without ticks: only the drift rule can refuse it.
    assert vet(inst.instance_id, tracked, {"u-a": _figure(101, start=(_T0 + 90, None))}) == {}
    # A process started two hours after its session began is a reused pid; logged.
    with caplog.at_level(logging.DEBUG, logger="clauster.runner"):
        late = {"u-b": _figure(102, start=(_T0 + 7200, 9))}
        assert vet(inst.instance_id, tracked, late) == {}
    assert "process started 7200s from its startedAt" in caplog.text
    # An epoch-only first read is never pinned, so it cannot poison a later full read.
    assert vet(inst.instance_id, tracked, {"u-c": _figure(103, start=(_T0, None))}) == {}
    assert "u-c" in vet(inst.instance_id, tracked, {"u-c": _figure(103, start=(_T0, 11))})


def test_same_start_rules():
    """Ticks decide; an epoch decides only where it is fixed at exec."""
    from clauster.runner import _same_start

    assert _same_start((1.0, 5), (9.0, 5), drift_prone=True)  # ticks win over the epoch
    assert not _same_start((1.0, 5), (1.0, 6), drift_prone=False)
    assert not _same_start((1.0, None), (1.0, None), drift_prone=True)
    assert _same_start((1.0, None), (1.01, None), drift_prone=False)
    assert not _same_start((1.0, None), (1.2, None), drift_prone=False)
    assert not _same_start((None, None), (None, None), drift_prone=False)


async def test_session_pins_are_forgotten_with_the_session_and_the_bridge(runner_config):
    """Pins do not outlive the session they guard or the bridge they belong to."""
    runner = _runner(runner_config)
    inst = _running(runner, project="alpha", pid=100)
    gone = _running(runner, project="beta", pid=200)
    a = {101: _session(101, "u-a", inst.instance_id)}
    runner._vet_session_figures(gone.instance_id, {}, {})
    runner._vet_session_figures(inst.instance_id, a, {"u-a": _figure(101)})
    assert runner._session_pins[inst.instance_id] == {("u-a", 101): (_T0, 500)}
    # The session left the tracked list: its pin goes, so a later session that the CLI
    # lists under the same uuid and pid is measured afresh.
    runner._vet_session_figures(inst.instance_id, {}, {})
    assert runner._session_pins[inst.instance_id] == {}
    del runner._instances[gone.instance_id]
    runner._vet_session_figures(inst.instance_id, {}, {})
    assert gone.instance_id not in runner._session_pins
