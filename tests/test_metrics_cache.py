"""Server-side metrics snapshot cache (#354): the runner samples off the request path."""

from __future__ import annotations

import asyncio
import logging
import os
import threading

import pytest

from clauster.models import InstanceStatus, RemoteControlInstance
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


async def test_refresh_samples_live_bridge_into_cache(runner_config):
    runner = _runner(runner_config)
    _running(runner)  # this test process's own pid → a real, live sample
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


async def test_refresh_replaces_wholesale_dropping_stale(runner_config):
    runner = _runner(runner_config)
    runner._metrics_cache = {"ghost": {"cpu_percent": 9.0}}  # a stale, now-gone bridge
    _running(runner)
    await runner._refresh_metrics_cache()
    assert "ghost" not in runner.metrics_snapshots()  # stale entry dropped
    assert runner.metrics_snapshot("alpha") is not None


async def test_refresh_drops_bridge_on_pid_reuse(runner_config):
    runner = _runner(runner_config)
    _running(runner, start=1.0)  # recorded start ≠ this pid's real create-time → reuse
    await runner._refresh_metrics_cache()
    assert runner.metrics_snapshot("alpha") is None


async def test_refresh_samples_when_pid_create_time_matches(runner_config):
    # start set AND matching the live pid's create-time → the guard passes, bridge sampled.
    from clauster import procutil

    runner = _runner(runner_config)
    _running(runner, start=procutil.proc_create_time(os.getpid()))
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

    monkeypatch.setattr(runner, "_refresh_metrics_cache", _boom)
    monkeypatch.setattr("clauster.runner.asyncio.sleep", _raise_cancelled)
    with pytest.raises(asyncio.CancelledError):  # only the sleep's cancel escapes
        await runner._metrics_refresh_forever()


async def test_refresh_forever_propagates_cancel_from_refresh(runner_config, monkeypatch):
    # A CancelledError from the refresh itself is re-raised (not swallowed by the loop),
    # so task cancellation stops the loop promptly.
    runner = _runner(runner_config)

    async def _cancel():
        raise asyncio.CancelledError

    monkeypatch.setattr(runner, "_refresh_metrics_cache", _cancel)
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
    assert runner.metrics_snapshot("alpha") is None
