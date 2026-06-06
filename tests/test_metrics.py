from __future__ import annotations

import os

import psutil

from clauster import metrics


def test_sample_tree_live_process_returns_aggregate():
    s = metrics.sample_tree(os.getpid(), interval=0.05)
    assert s is not None
    assert s["procs"] >= 1
    assert s["cpu_percent"] >= 0.0
    assert s["rss_bytes"] > 0
    # disk fields are ints where io_counters is supported (Linux/Windows) or None (macOS).
    assert s["disk_read_bps"] is None or isinstance(s["disk_read_bps"], int)
    assert s["disk_write_bps"] is None or isinstance(s["disk_write_bps"], int)


def test_sample_tree_gone_pid_returns_none():
    # Above any platform's pid_max → guaranteed NoSuchProcess.
    assert metrics.sample_tree(2_147_483_646, interval=0.01) is None


def test_sample_tree_disk_unavailable_yields_none(monkeypatch):
    # Simulate a platform without io_counters (e.g. macOS): disk fields fall to None.
    def boom(self):
        raise NotImplementedError

    monkeypatch.setattr(psutil.Process, "io_counters", boom, raising=False)
    s = metrics.sample_tree(os.getpid(), interval=0.02)
    assert s is not None
    assert s["disk_read_bps"] is None
    assert s["disk_write_bps"] is None


def test_sample_tree_survives_vanishing_procs(monkeypatch):
    # If procs raise while being read (they exited mid-sample), the walk skips
    # them and still returns a sample rather than crashing.
    def boom(self):
        raise psutil.NoSuchProcess(self.pid)

    monkeypatch.setattr(psutil.Process, "cpu_times", boom)
    s = metrics.sample_tree(os.getpid(), interval=0.01)
    assert s is not None
    assert s["cpu_percent"] == 0.0
    assert s["rss_bytes"] == 0
