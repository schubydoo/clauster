from __future__ import annotations

import itertools
import os
from collections import namedtuple

import psutil

from clauster import metrics


def test_sample_tree_live_process_returns_aggregate():
    s = metrics.sample_tree(os.getpid(), interval=0.05)
    assert s is not None
    assert s["procs"] >= 1
    assert s["cpu_percent"] >= 0.0
    assert s["cpu_normalized"] is False
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


def test_sample_tree_disk_available_computes_positive_rates(monkeypatch):
    # Force the io_counters-SUPPORTED branch on EVERY OS. macOS's psutil has no
    # io_counters, so reading io.read_bytes/write_bytes and computing the disk-rate
    # delta only ever run on the Linux/Windows cells — the macOS coverage flag shows
    # them uncovered. Returning monotonically growing byte counters makes the second
    # snapshot show a positive delta, exercising the supported path deterministically
    # regardless of host platform (mirrors the disk-unavailable test above).
    pio = namedtuple("pio", ["read_bytes", "write_bytes"])
    counter = itertools.count(1_000_000, 1_000_000)

    def growing(self):
        n = next(counter)
        return pio(read_bytes=n, write_bytes=n)

    monkeypatch.setattr(psutil.Process, "io_counters", growing, raising=False)
    s = metrics.sample_tree(os.getpid(), interval=0.02)
    assert s is not None
    assert isinstance(s["disk_read_bps"], int)
    assert s["disk_read_bps"] > 0
    assert isinstance(s["disk_write_bps"], int)
    assert s["disk_write_bps"] > 0


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


def test_sample_tree_normalize_cpu_divides_by_cores(monkeypatch):
    # normalize_cpu divides the summed figure by the core count and flags it.
    monkeypatch.setattr(psutil, "cpu_count", lambda: 4)
    s = metrics.sample_tree(os.getpid(), interval=0.05, normalize_cpu=True)
    assert s is not None
    assert s["cpu_normalized"] is True
    assert s["cpu_percent"] >= 0.0


def test_sample_tree_proc_vanishes_during_second_snapshot(monkeypatch):
    # memory_info is read only in the second snapshot; raising there exercises the
    # mid-sample skip (a proc exiting between snapshots) without faulting the sample.
    def boom(self):
        raise psutil.NoSuchProcess(self.pid)

    monkeypatch.setattr(psutil.Process, "memory_info", boom)
    s = metrics.sample_tree(os.getpid(), interval=0.01)
    assert s is not None
    assert s["rss_bytes"] == 0
