"""Server-side metrics snapshot cache (#354): the runner samples off the request path."""

from __future__ import annotations

import asyncio
import logging
import os

import pytest

from clauster.models import InstanceStatus, RemoteControlInstance
from clauster.runner import SessionRunner


def _runner(runner_config) -> SessionRunner:
    config, claude_json = runner_config
    return SessionRunner(config, claude_json=claude_json)


def _running(runner, project="alpha", *, pid=None, start=None):
    inst = RemoteControlInstance(project=project, label=project)
    inst.status = InstanceStatus.RUNNING
    inst.bridge_pid = os.getpid() if pid is None else pid
    inst.bridge_proc_start = start
    runner._instances[project] = inst
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


async def _raise_cancelled(_seconds):
    raise asyncio.CancelledError


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
