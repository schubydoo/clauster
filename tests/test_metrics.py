from __future__ import annotations

import contextlib
import itertools
import logging
import os
import subprocess
import sys
import time
from collections import namedtuple

import psutil
import pytest

from clauster import metrics


def test_sample_tree_live_process_returns_aggregate():
    s = metrics.sample_tree(os.getpid(), interval=0.05, sessions={})
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
    assert metrics.sample_tree(2_147_483_646, interval=0.01, sessions={}) is None


def test_sample_tree_root_vanishes_before_the_children_walk_returns_none(monkeypatch):
    # metrics.py 60: `psutil.Process(pid)` succeeds, then the root exits before
    # `root.children(recursive=True)` runs, so the walk raises a _GONE member. That second
    # `except _GONE: return None` arm — distinct from the no-btime RuntimeError arm (#1429,
    # already covered) — returns None rather than faulting the whole sample.
    def boom(self, recursive=False):
        raise psutil.NoSuchProcess(self.pid)

    monkeypatch.setattr(psutil.Process, "children", boom)
    assert metrics.sample_tree(os.getpid(), interval=0.01, sessions={}) is None


def test_sample_tree_disk_unavailable_yields_none(monkeypatch):
    # Simulate a platform without io_counters (e.g. macOS): disk fields fall to None.
    def boom(self):
        raise NotImplementedError

    monkeypatch.setattr(psutil.Process, "io_counters", boom, raising=False)
    s = metrics.sample_tree(os.getpid(), interval=0.02, sessions={})
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
    s = metrics.sample_tree(os.getpid(), interval=0.02, sessions={})
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
    s = metrics.sample_tree(os.getpid(), interval=0.01, sessions={})
    assert s is not None
    assert s["cpu_percent"] == 0.0
    assert s["rss_bytes"] == 0


def test_sample_tree_normalize_cpu_divides_by_cores(monkeypatch):
    # normalize_cpu divides the summed figure by the core count and flags it.
    monkeypatch.setattr(psutil, "cpu_count", lambda: 4)
    s = metrics.sample_tree(os.getpid(), interval=0.05, normalize_cpu=True, sessions={})
    assert s is not None
    assert s["cpu_normalized"] is True
    assert s["cpu_percent"] >= 0.0


def test_sample_tree_proc_vanishes_during_second_snapshot(monkeypatch):
    # memory_info is read only in the second snapshot; raising there exercises the
    # mid-sample skip (a proc exiting between snapshots) without faulting the sample.
    def boom(self):
        raise psutil.NoSuchProcess(self.pid)

    monkeypatch.setattr(psutil.Process, "memory_info", boom)
    s = metrics.sample_tree(os.getpid(), interval=0.01, sessions={})
    assert s is not None
    assert s["rss_bytes"] == 0


def test_sample_tree_children_btime_runtimeerror_samples_root_only(monkeypatch, caplog):
    # psutil's children() reads the epoch (create_time without monotonic), which raises a
    # bare RuntimeError on a procfs with no `btime` line (gVisor, WSL1, some containers).
    # The walk must degrade to the root alone rather than fault the whole sample. Without
    # the RuntimeError arm this call raises RuntimeError instead of returning a sample.
    def boom(self, recursive=False):
        raise RuntimeError("line 'btime' not found in /proc/stat")

    monkeypatch.setattr(psutil.Process, "children", boom)
    with caplog.at_level(logging.DEBUG, logger="clauster.metrics"):
        s = metrics.sample_tree(os.getpid(), interval=0.02, sessions={})
    assert s is not None
    assert s["procs"] == 1
    assert s["rss_bytes"] > 0
    # The degrade leaves a trace, so a masked non-btime RuntimeError stays recoverable.
    assert "degrades to root only" in caplog.text


# ----- per-session breakdown (backlog TASK-342) ----------------------------------

# A three-level tree: bridge stand-in → session stand-in → one tool process under it. Each
# level sleeps, so the shape holds still while the sampler walks it.
_GRANDCHILD = "import time; time.sleep(60)"
_CHILD = (
    "import subprocess, sys, time; "
    f"subprocess.Popen([sys.executable, '-c', {_GRANDCHILD!r}]); time.sleep(60)"
)
_ROOT = (
    "import subprocess, sys, time; "
    f"subprocess.Popen([sys.executable, '-c', {_CHILD!r}]); time.sleep(60)"
)


@pytest.fixture
def bridge_tree():
    """Yield ``(root_pid, session_pid, session_procs)`` of a real three-level process tree.

    A dedicated tree, never the pytest worker: sampling the worker walks the dying
    subprocesses of sibling tests, which faulted the xdist worker on Windows (see the
    ``live_pid`` fixture in ``test_metrics_cache.py``).
    """
    root = subprocess.Popen(
        [sys.executable, "-c", _ROOT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    proc = psutil.Process(root.pid)
    try:
        # Wait for the deepest level, then for the tree to hold still: on Windows a venv's
        # python.exe is a launcher that starts the real interpreter as one more child, so
        # the tree can be deeper than three and still be growing when the leaf first shows.
        deadline = time.monotonic() + 30
        seen: list[int] = []
        steady = 0
        while steady < 3:
            assert time.monotonic() < deadline, "process tree did not come up"
            time.sleep(0.1)
            with contextlib.suppress(psutil.Error):
                now = sorted(p.pid for p in proc.children(recursive=True))
                leaf = any(
                    "subprocess" not in " ".join(p.cmdline())
                    for p in proc.children(recursive=True)
                )
                steady = steady + 1 if (leaf and now == seen) else 0
                seen = now
        session = proc.children()[0]
        yield root.pid, session.pid, len(session.children(recursive=True)) + 1
    finally:
        doomed = [proc]
        with contextlib.suppress(psutil.Error):
            doomed = [*proc.children(recursive=True), proc]
        for p in doomed:
            with contextlib.suppress(psutil.Error):
                p.kill()
        root.wait()


def test_sample_tree_reports_a_session_subtree_inside_the_bridge_total(bridge_tree):
    root, session, session_procs = bridge_tree
    s = metrics.sample_tree(root, interval=0.05, sessions={session: "uuid-a"})
    assert s is not None
    entry = s["sessions"]["uuid-a"]
    assert entry["pid"] == session
    # The session and the tool it started, not the bridge.
    assert entry["procs"] == session_procs >= 2
    assert 0 < entry["rss_bytes"] < s["rss_bytes"]  # a part of the total, never all of it
    assert 0.0 <= entry["cpu_percent"] <= s["cpu_percent"]


def test_sample_tree_leaves_out_a_session_pid_outside_the_tree(bridge_tree):
    # A pid the CLI named that is not under this bridge, and the bridge pid itself, get
    # no figure. The bridge total is still returned.
    root, _session, _procs = bridge_tree
    s = metrics.sample_tree(
        root, interval=0.02, sessions={2_147_483_646: "elsewhere", root: "the-bridge"}
    )
    assert s is not None
    assert s["sessions"] == {}
    assert s["rss_bytes"] > 0


def test_sample_tree_leaves_out_a_session_that_exits_mid_sample(bridge_tree, monkeypatch):
    # The session root fails its second read (it exited between the snapshots). Its
    # orphaned tool must not be reported as the whole session.
    root, session, session_procs = bridge_tree
    real = psutil.Process.memory_info

    def gone_for_session(self):
        if self.pid == session:
            raise psutil.NoSuchProcess(self.pid)
        return real(self)

    monkeypatch.setattr(psutil.Process, "memory_info", gone_for_session)
    s = metrics.sample_tree(root, interval=0.02, sessions={session: "uuid-a"})
    assert s is not None
    assert s["sessions"] == {}


def test_sample_tree_breakdown_follows_normalize_cpu(bridge_tree, monkeypatch):
    root, session, session_procs = bridge_tree
    monkeypatch.setattr(psutil, "cpu_count", lambda: 4)
    s = metrics.sample_tree(root, interval=0.02, normalize_cpu=True, sessions={session: "u"})
    assert s is not None
    assert s["cpu_normalized"] is True
    assert s["sessions"]["u"]["cpu_percent"] <= s["cpu_percent"]


def test_sample_tree_skips_the_ppid_read_without_sessions(monkeypatch):
    # A plain sample costs what it did before the breakdown existed: no ppid reads.
    def boom(self):
        raise AssertionError("ppid read without a breakdown")

    monkeypatch.setattr(psutil.Process, "ppid", boom)
    assert metrics.sample_tree(os.getpid(), interval=0.01, sessions={}) is not None


def test_session_owners_nearest_session_wins_and_figures_never_overlap():
    # 1 = bridge. 10 = session A, 11 its tool. 20 = session B started inside A's tool,
    # 21 its tool. 30 = a helper of the bridge itself, owned by no session.
    parent = {10: 1, 11: 10, 20: 11, 21: 20, 30: 1}
    owners = metrics._session_owners(1, parent, {10, 20})
    assert owners == {10: 10, 11: 10, 20: 20, 21: 20}


def test_session_owners_terminates_on_a_ppid_cycle():
    # A cycle can only come from pids recycled between reads; the walk is bounded.
    assert metrics._session_owners(1, {5: 6, 6: 5}, {99}) == {}
