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

from clauster import metrics, procutil


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

# A real tree: bridge stand-in → N session stand-ins → one tool process under each. Every
# level sleeps, so the shape holds still while the sampler walks it. Each level is named by
# its whole `-c` code, which only that level runs.
_TOOL = "import time; time.sleep(60)"
_SESSION = (
    "import subprocess, sys, time; "
    f"subprocess.Popen([sys.executable, '-c', {_TOOL!r}]); time.sleep(61)"
)


def _root_code(n: int) -> str:
    return (
        "import subprocess, sys, time; "
        f"[subprocess.Popen([sys.executable, '-c', {_SESSION!r}]) for _ in range({n})]; "
        "time.sleep(62)"
    )


def _code(p: psutil.Process) -> str:
    with contextlib.suppress(psutil.Error, IndexError):
        return p.cmdline()[-1]
    return ""


def _topmost(procs: list[psutil.Process], code: str) -> list[psutil.Process]:
    # On Windows a venv's python.exe is a launcher that starts the real interpreter as a
    # child with the same argv, so one level can be two processes: take the upper one.
    out = []
    for p in procs:
        if _code(p) != code:
            continue
        with contextlib.suppress(psutil.Error):
            parent = p.parent()
            if parent is None or _code(parent) != code:
                out.append(p)
    return out


@contextlib.contextmanager
def _tree(n: int):
    """Yield ``(root_pid, [(session_pid, session_procs), ...])`` of a real process tree.

    A dedicated tree, never the pytest worker: sampling the worker walks the dying
    subprocesses of sibling tests, which faulted the xdist worker on Windows (see the
    ``live_pid`` fixture in ``test_metrics_cache.py``).
    """
    root = subprocess.Popen(
        [sys.executable, "-c", _root_code(n)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc = psutil.Process(root.pid)
    try:
        # Wait for every tool, then for the tree to hold still.
        deadline = time.monotonic() + 30
        seen: list[int] = []
        steady = 0
        while steady < 3:
            assert time.monotonic() < deadline, "process tree did not come up"
            time.sleep(0.1)
            with contextlib.suppress(psutil.Error):
                procs = proc.children(recursive=True)
                now = sorted(p.pid for p in procs)
                ready = len(_topmost(procs, _TOOL)) == n
                steady = steady + 1 if (ready and now == seen) else 0
                seen = now
        sessions = _topmost(proc.children(recursive=True), _SESSION)
        yield root.pid, [(s.pid, len(s.children(recursive=True)) + 1) for s in sessions]
    finally:
        doomed = [proc]
        with contextlib.suppress(psutil.Error):
            doomed = [*proc.children(recursive=True), proc]
        for p in doomed:
            with contextlib.suppress(psutil.Error):
                p.kill()
        root.wait()


@pytest.fixture
def bridge_tree():
    """Yield ``(root_pid, session_pid, session_procs)`` for a bridge with one session."""
    with _tree(1) as (root, [(session, procs)]):
        yield root, session, procs


@pytest.fixture
def two_session_tree():
    """Yield ``(root_pid, [(session_pid, session_procs), ...])`` for two sibling sessions."""
    with _tree(2) as tree:
        yield tree


def _fixed_cpu(monkeypatch, per_read: float = 0.01) -> None:
    # Each process reads 0.0 cpu-seconds in the first snapshot and `per_read` in the
    # second, so every process contributes the same, exactly known delta.
    reads: dict[int, int] = {}
    times = namedtuple("times", ["user", "system"])

    def cpu_times(self):
        n = reads.get(self.pid, 0)
        reads[self.pid] = n + 1
        return times(user=per_read * n, system=0.0)

    monkeypatch.setattr(psutil.Process, "cpu_times", cpu_times)


def test_sample_tree_reports_a_session_subtree_inside_the_bridge_total(bridge_tree):
    root, session, session_procs = bridge_tree
    s = metrics.sample_tree(root, interval=0.05, sessions={session: "uuid-a"})
    assert s is not None
    entry = s["sessions"]["uuid-a"]
    assert entry["pid"] == session
    # The session and the tool it started, not the bridge.
    assert entry["procs"] == session_procs >= 2
    assert 0 < entry["rss_bytes"] < s["rss_bytes"]  # a part of the total, never all of it
    # The identity the runner pins: the session process's own start.
    epoch, ticks = entry["start"]
    want_epoch, want_ticks = procutil.proc_start_pair(session)
    assert ticks == want_ticks
    assert epoch is not None and abs(epoch - want_epoch) < 2


def test_sample_tree_session_cpu_is_its_own_subtree_exactly(bridge_tree, monkeypatch):
    # 0.01 cpu-seconds per process over a 0.1s window is 10% each, so the figures are
    # exact: the session gets its own processes' share, never the bridge total.
    root, session, session_procs = bridge_tree
    _fixed_cpu(monkeypatch)
    s = metrics.sample_tree(root, interval=0.1, sessions={session: "u"})
    assert s is not None
    assert s["cpu_percent"] == 10.0 * s["procs"]
    assert s["sessions"]["u"]["cpu_percent"] == 10.0 * session_procs
    assert s["sessions"]["u"]["cpu_percent"] < s["cpu_percent"]


def test_sample_tree_breakdown_follows_normalize_cpu(bridge_tree, monkeypatch):
    root, session, session_procs = bridge_tree
    _fixed_cpu(monkeypatch)
    monkeypatch.setattr(psutil, "cpu_count", lambda: 4)
    s = metrics.sample_tree(root, interval=0.1, normalize_cpu=True, sessions={session: "u"})
    assert s is not None
    assert s["cpu_normalized"] is True
    assert s["cpu_percent"] == round(10.0 * s["procs"] / 4, 1)
    assert s["sessions"]["u"]["cpu_percent"] == round(10.0 * session_procs / 4, 1)


def test_sample_tree_keeps_sibling_sessions_apart(two_session_tree, monkeypatch):
    # Two sessions under one bridge each get their own processes only.
    root, sessions = two_session_tree
    assert len(sessions) == 2
    _fixed_cpu(monkeypatch)
    s = metrics.sample_tree(
        root, interval=0.1, sessions={pid: f"u{i}" for i, (pid, _) in enumerate(sessions)}
    )
    assert s is not None
    for i, (pid, procs) in enumerate(sessions):
        entry = s["sessions"][f"u{i}"]
        assert entry["pid"] == pid
        assert entry["procs"] == procs
        assert entry["cpu_percent"] == 10.0 * procs
    # Together the sessions are part of the bridge total, which also has the bridge itself.
    assert sum(e["procs"] for e in s["sessions"].values()) < s["procs"]
    assert sum(e["rss_bytes"] for e in s["sessions"].values()) < s["rss_bytes"]


def test_sample_tree_leaves_out_a_session_pid_outside_the_tree(bridge_tree, monkeypatch):
    # A pid the CLI named that is not under this bridge (a live one, and one that does not
    # exist), and the bridge pid itself, get no figure and no subtree walk. The bridge
    # total is still returned.
    root, _session, _procs = bridge_tree
    # Live and outside the bridge stand-in's tree. A childless sleeper, not the pytest
    # worker: if the guard ever broke, walking the worker is what faulted Windows CI.
    outside = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    real = psutil.Process.children
    walked: list[int] = []

    def walk(self, recursive=False):
        walked.append(self.pid)
        return real(self, recursive=recursive)

    monkeypatch.setattr(psutil.Process, "children", walk)
    try:
        s = metrics.sample_tree(
            root,
            interval=0.02,
            sessions={outside.pid: "outside", 2_147_483_646: "gone", root: "the-bridge"},
        )
    finally:
        outside.kill()
        outside.wait()
    assert s is not None
    assert s["sessions"] == {}
    assert s["rss_bytes"] > 0
    assert walked == [root]


def test_sample_tree_nested_session_keeps_its_own_processes(bridge_tree):
    # A session started inside another session's subtree (here: the tool, named as a
    # session too) reports its own processes, and the outer session no longer counts them.
    root, session, session_procs = bridge_tree
    inner = psutil.Process(session).children()[0]
    inner_procs = len(inner.children(recursive=True)) + 1
    s = metrics.sample_tree(root, interval=0.02, sessions={session: "outer", inner.pid: "inner"})
    assert s is not None
    assert s["sessions"]["inner"]["procs"] == inner_procs
    assert s["sessions"]["outer"]["procs"] == session_procs - inner_procs


def test_sample_tree_drops_a_session_whose_identity_changed_mid_sample(bridge_tree, monkeypatch):
    # The pid named a different process after the snapshots than before them (it died and
    # was reused inside the window): no figure under the old identity.
    root, session, _procs = bridge_tree
    reads = itertools.count()

    def pair(pid):
        return (1.0, 100 + next(reads))  # a new tick count on every read

    monkeypatch.setattr(procutil, "proc_start_pair", pair)
    s = metrics.sample_tree(root, interval=0.02, sessions={session: "u"})
    assert s is not None
    assert s["sessions"] == {}


def test_start_token_uses_the_half_that_is_stable():
    assert metrics._start_token((1.0, 7)) == metrics._start_token((2.0, 7))  # ticks decide
    assert metrics._start_token((1.0, None)) != metrics._start_token((2.0, None))


def test_sample_tree_leaves_out_a_session_that_exits_mid_sample(bridge_tree, monkeypatch):
    # The session root fails its second read (it exited between the snapshots). Its
    # orphaned tool must not be reported as the whole session.
    root, session, _procs = bridge_tree
    real = psutil.Process.memory_info

    def gone_for_session(self):
        if self.pid == session:
            raise psutil.NoSuchProcess(self.pid)
        return real(self)

    monkeypatch.setattr(psutil.Process, "memory_info", gone_for_session)
    s = metrics.sample_tree(root, interval=0.02, sessions={session: "uuid-a"})
    assert s is not None
    assert s["sessions"] == {}


def test_sample_tree_counts_a_session_whose_tool_exits_mid_sample(bridge_tree, monkeypatch):
    # A tool that exits between the snapshots drops out of the session figure; the session
    # itself still reports, without it.
    root, session, session_procs = bridge_tree
    tool = psutil.Process(session).children(recursive=True)[0].pid
    real = psutil.Process.memory_info

    def gone_for_tool(self):
        if self.pid == tool:
            raise psutil.NoSuchProcess(self.pid)
        return real(self)

    monkeypatch.setattr(psutil.Process, "memory_info", gone_for_tool)
    s = metrics.sample_tree(root, interval=0.02, sessions={session: "u"})
    assert s is not None
    assert s["sessions"]["u"]["procs"] == session_procs - 1


def test_sample_tree_leaves_out_a_session_whose_walk_fails(bridge_tree, monkeypatch):
    # The session's own children walk fails (it exited, or a procfs with no btime line):
    # no figure, and the bridge total is still returned.
    root, session, _procs = bridge_tree
    real = psutil.Process.children

    def walk(self, recursive=False):
        if self.pid == session:
            raise RuntimeError("line 'btime' not found in /proc/stat")
        return real(self, recursive=recursive)

    monkeypatch.setattr(psutil.Process, "children", walk)
    s = metrics.sample_tree(root, interval=0.02, sessions={session: "u"})
    assert s is not None
    assert s["sessions"] == {}
    assert s["procs"] > 1


def test_sample_tree_walks_no_session_subtree_without_sessions(bridge_tree, monkeypatch):
    # A plain sample costs what it did before the breakdown existed: one children walk,
    # the bridge's own, and no identity reads.
    root, _session, _procs = bridge_tree
    real = psutil.Process.children
    walked: list[int] = []

    def walk(self, recursive=False):
        walked.append(self.pid)
        return real(self, recursive=recursive)

    def boom(pid):
        raise AssertionError("identity read without a breakdown")

    monkeypatch.setattr(psutil.Process, "children", walk)
    monkeypatch.setattr(procutil, "proc_start_pair", boom)
    assert metrics.sample_tree(root, interval=0.01, sessions={}) is not None
    assert walked == [root]


def test_own_members_nearest_session_wins_and_figures_never_overlap():
    # 10 = session A with tool 11. 20 = session B started inside A's tool, with tool 21.
    subtrees = {10: {10, 11, 20, 21}, 20: {20, 21}}
    assert metrics._own_members(subtrees) == {10: {10, 11}, 20: {20, 21}}


def test_own_members_mutual_containment_reports_neither_root():
    # Only a recycled pid can make two roots contain each other; neither keeps its root,
    # so neither gets a figure.
    own = metrics._own_members({10: {10, 20}, 20: {20, 10}})
    assert 10 not in own[10] and 20 not in own[20]
